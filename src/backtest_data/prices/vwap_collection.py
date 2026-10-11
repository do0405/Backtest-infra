"""Bounded full-universe trade acquisition, with SQL pages as checkpoints."""
import argparse
from datetime import date, timedelta
import json, time, shutil
from pathlib import Path
from sqlalchemy import select, text
from ..config import Settings
from ..credentials import load_credentials
from ..db import make_engine, transaction, utcnow
from ..ingestion.models import TargetUniverseManifest
from ..ingestion.http import ProviderError
from ..supplemental.transport import SourceHTTP
from ..supplemental.alpaca_inputs import decode_alpaca_json
from .vwap_inputs import trade_day_request, source_trade_symbol, RULE, IDENTITY_POLICY, INPUT_POLICY
from .vwap_input_models import VWAPTradeRun
from .vwap_sql import create_trade_run, commit_trade_page


def request_failure_action(code,page):
    return 'record_request_gap' if code in {'HTTP_400','HTTP_404'} and page==0 else 'blocked'


def collect_one_trade_page(engine, http, credentials, raw_root, *, security_id, symbol, session,source_member=None):
    with transaction(engine) as db:
        run=create_trade_run(db,security_id=security_id,symbol=symbol,session=session,source_member=source_member)
        identity=run.id
        if run.status in {'complete','unqualified'}:
            return {'run_id':identity,'status':run.status,'pages':run.cursor['page'],
                'statistic_id':run.cursor.get('statistic_id'),'coverage_complete':False}
    with engine.connect() as lock:
        key='vwap_trade_session:'+identity
        owned=lock.execute(text('SELECT pg_try_advisory_lock(hashtext(:key))'),{'key':key}).scalar_one()
        lock.commit()
        if not owned:return {'status':'already_running','coverage_complete':False}
        try:
            with transaction(engine) as db:
                run=db.get(VWAPTradeRun,identity)
                if run.status in {'complete','unqualified'}:
                    return {'run_id':identity,'status':run.status,'coverage_complete':False}
                params=trade_day_request(symbol,session,token=run.cursor['next_page_token'],limit=run.cursor['limit'])
            response=http.fetch('alpaca','GET','https://data.alpaca.markets/v2/stocks/'+symbol+'/trades',
                params=params,headers={'APCA-API-KEY-ID':credentials.alpaca_key.reveal(),
                    'APCA-API-SECRET-KEY':credentials.alpaca_secret.reveal()})
            payload=decode_alpaca_json(response.content)
            with transaction(engine) as db:
                run=db.scalar(select(VWAPTradeRun).where(VWAPTradeRun.id==identity).with_for_update())
                return commit_trade_page(db,run,params=params,response=response,payload=payload,raw_root=raw_root)
        finally:
            lock.execute(text('SELECT pg_advisory_unlock(hashtext(:key))'),{'key':key});lock.commit()


def collect_vwap(engine, settings, *, manifest_id, progress_path, budget_seconds=600, max_pages=None):
    import math
    if isinstance(budget_seconds,bool) or not isinstance(budget_seconds,(int,float)) or not math.isfinite(budget_seconds) or budget_seconds<=0:
        raise ValueError('positive_VWAP_execution_budget_required')
    if max_pages is not None and (type(max_pages) is not int or max_pages<1):
        raise ValueError('positive_bounded_page_budget_required')
    path=Path(progress_path)
    with transaction(engine) as db:
        manifest=db.get(TargetUniverseManifest,manifest_id)
        if manifest is None:raise ValueError('original_target_manifest_required')
        members=[dict(m) for m in manifest.members if m['market']=='US']
        start=date.fromisoformat(manifest.scope['output_start']);end=date.fromisoformat(manifest.scope['output_end'])
        target_count=len(manifest.members);target_hash=manifest.content_hash
    state=json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {
        'manifest_id':manifest_id,'target_hash':target_hash,'rule':RULE,'identity_policy':IDENTITY_POLICY,
        'input_policy':INPUT_POLICY,'session':end.isoformat(),
        'member_index':0,'pages':0,'complete_sessions':0,'unqualified_sessions':0,'unavailable_sessions':0,
        'coverage_complete':False,'original_target_count':target_count,'market_target_count':len(members),
        'output_start':start.isoformat(),'output_end':end.isoformat()}
    if (state['manifest_id'],state['target_hash'],state['rule'],state.get('identity_policy'),state.get('input_policy'),state['output_start'],state['output_end'])!=(
            manifest_id,target_hash,RULE,IDENTITY_POLICY,INPUT_POLICY,start.isoformat(),end.isoformat()):
        raise ValueError('VWAP_progress_scope_mismatch')
    def save(**values):
        state.update(values,checked_at=utcnow().isoformat())
        path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix('.pending.json')
        temp.write_text(json.dumps(state,indent=2),encoding='utf-8');temp.replace(path)
    credentials=load_credentials(sources=['alpaca'])
    if not credentials.alpaca_key or not credentials.alpaca_secret:raise ValueError('alpaca_config_required')
    http=SourceHTTP(engine);started=time.monotonic();attempted=0
    with engine.connect() as lock:
        key='vwap_backfill:'+manifest_id
        owned=lock.execute(text('SELECT pg_try_advisory_lock(hashtext(:key))'),{'key':key}).scalar_one();lock.commit()
        if not owned:http.close();return {'status':'already_running','coverage_complete':False}
        try:
            while time.monotonic()-started<budget_seconds and (max_pages is None or attempted<max_pages):
                if shutil.disk_usage(settings.raw_root.resolve().anchor).free<50*1024**3:
                    save(status='disk_floor_deferred');break
                day=date.fromisoformat(state['session'])
                if day<start:save(status='request_traversal_complete');break
                if day.weekday()>=5:
                    save(session=(day-timedelta(days=1)).isoformat(),member_index=0);continue
                if state['member_index']>=len(members):
                    save(session=(day-timedelta(days=1)).isoformat(),member_index=0);continue
                member=members[state['member_index']]
                provider_symbol=source_trade_symbol(member)
                save(status='collecting',current_symbol=member['symbol'],provider_symbol=provider_symbol,reason=None)
                try:
                    result=collect_one_trade_page(engine,http,credentials,settings.raw_root,
                        security_id=member['security_id'],symbol=provider_symbol,session=day,source_member=member)
                except ProviderError as exc:
                    with transaction(engine) as db:
                        run=create_trade_run(db,security_id=member['security_id'],symbol=provider_symbol,
                            session=day,source_member=member)
                        page=run.cursor['page']
                    if request_failure_action(exc.code,page)=='record_request_gap':
                        from ..releases.models import ValidationIssue
                        from ..db import new_id
                        with transaction(engine) as db:
                            db.add(ValidationIssue(id=new_id(),record_domain='security',record_id=member['security_id'],
                                rule='vwap_initial_request_gap_v1',severity='warning',created_at=utcnow(),
                                evidence={'source':'alpaca','source_symbol':provider_symbol,'catalog_symbol':member['symbol'],
                                    'session':str(day),'http_status':exc.code,'reason':'initial_security_day_request_rejected',
                                    'full_period_unsupported':False,'coverage_complete':False}))
                        save(unavailable_sessions=state['unavailable_sessions']+1,
                             member_index=state['member_index']+1,last_gap={'symbol':member['symbol'],'session':str(day),'reason':exc.code})
                        attempted+=1;continue
                    save(status='blocked',reason=exc.code);break
                if result['status']=='already_running':save(status='dependency_wait');break
                attempted+=1
                state['pages']+=1
                if result['status'] in {'complete','unqualified'}:
                    counter='complete_sessions' if result['status']=='complete' else 'unqualified_sessions'
                    state[counter]+=1;state['member_index']+=1
                save(last_commit_at=utcnow().isoformat(),last_result=result)
            if state.get('status')=='collecting':save(status='budget_deferred')
            return state
        finally:
            http.close()
            lock.execute(text('SELECT pg_advisory_unlock(hashtext(:key))'),{'key':key});lock.commit()


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--manifest-id',required=True);p.add_argument('--progress-path',required=True,type=Path)
    p.add_argument('--budget-seconds',type=int,default=600);p.add_argument('--max-pages',type=int)
    args=p.parse_args();settings=Settings.from_env();engine=make_engine(settings.database_url)
    try:
        print(json.dumps(collect_vwap(engine,settings,manifest_id=args.manifest_id,progress_path=args.progress_path,
            budget_seconds=args.budget_seconds,max_pages=args.max_pages),indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({'status':'blocked','error_type':type(exc).__name__,'details_redacted':True}));return 2
    finally:engine.dispose()


if __name__=='__main__':raise SystemExit(main())
