"""Explicit bounded remote adapters for collectors outside the core supervisor.

Only a fresh, explicitly owned remote worker may call these adapters. The full
original manifest remains the denominator; operational SQL state is recreated
from validated portable checkpoints, never imported as arbitrary table data.
"""
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import copy
import json
import math
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..db import transaction, utcnow
from ..ingestion.models import CollectionJob, TargetUniverseManifest
from ..prices.krx_quote_collection import plan_krx_quotes, run_krx_quote_collection
from ..prices.krx_quote_storage import KRX_SERVICES
from ..prices.provider_basis import classify_price_basis
from ..prices.supplier_collection import collect_supplier_adjusted
from ..prices.supplier_models import SupplierPriceCollectionState
from ..prices.supplier_refresh_models import SupplierPriceRefreshState
from ..ops.source_cadence import provider_ready_date, krx_original_complete
from .codec import PaginationCursor, dumps

AUTHORITY = 'new_remote_ephemeral_database'
ORIGINAL_START = date(2016,1,1)
ORIGINAL_END = date(2026,10,7)
SUPPLEMENTAL_DEPENDENCIES = frozenset({'sec','alpaca','alpha_vantage'})
SUPPLIER_CURSOR_FIELDS = frozenset({'member_index','pages','inserted','source_rows','rejected',
    'completed_members','empty_members','unsupported_members','request_anchor','page_token'})
HANDLERS = frozenset({'krx_quotes','supplemental','supplier_adjusted'})


def _authority(authority):
    if authority != AUTHORITY:raise ValueError('explicit fresh remote authority required')


def _budget(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<value<=14400:
        raise ValueError('positive bounded source execution budget required')
    return value


def _aware(value):
    if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('aware source scheduling time required')
    return value


def full_manifest_scope(manifest):
    if manifest is None or not manifest.id or len(manifest.members)!=17758:
        raise ValueError('whole original target manifest required')
    if (manifest.scope.get('output_start'),manifest.scope.get('output_end'))!=('2016-01-01','2026-10-07'):
        raise ValueError('original backfill dates must remain intact')
    counts=dict(Counter(row.get('market') for row in manifest.members))
    if counts!={'KR':4507,'US':13251}:raise ValueError('whole KR/US source target required')
    if not re.fullmatch('[a-f0-9]{64}',manifest.content_hash):raise ValueError('original target hash required')
    return {'manifest_id':manifest.id,'target_hash':manifest.content_hash,'target_count':17758,
        'market_target_counts':counts,'output_start':'2016-01-01','output_end':'2026-10-07'}


def _binding(state,scope):
    if not isinstance(state,dict) or state.get('scope')!=scope:raise ValueError('source checkpoint scope binding mismatch')


def _window(window):
    if (not isinstance(window,dict) or set(window)!={'start','end'} or any(type(day) is not date for day in window.values())
            or window['start']<ORIGINAL_START or window['start']>window['end']):
        raise ValueError('explicit ordered source refresh window required')
    return window


def _window_key(window):
    _window(window)
    return window['start'].isoformat()+'_'+window['end'].isoformat()


def _window_state(previous,scope):
    if previous is None:return {'scope':copy.deepcopy(scope),'version':2,'backfill':None,'refresh_windows':{},'coverage_complete':False}
    _binding(previous,scope)
    if previous.get('version')!=2:
        return {'scope':copy.deepcopy(scope),'version':2,'backfill':copy.deepcopy(previous),'refresh_windows':{},'coverage_complete':False}
    result=copy.deepcopy(previous)
    if not isinstance(result.get('refresh_windows'),dict):raise ValueError('bounded refresh checkpoint mapping required')
    if result.get('backfill') is not None:_binding(result['backfill'],scope)
    for key,value in result['refresh_windows'].items():
        _binding(value,scope)
        if _window_key(value.get('requested_window'))!=key:raise ValueError('refresh checkpoint window binding mismatch')
    result['coverage_complete']=False
    return result


def refresh_work_windows(state,*,as_of):
    """Newest daily data, oldest unfinished refresh, and original backfill."""
    _aware(as_of)
    completed=min(as_of.astimezone(ZoneInfo(zone)).date()-timedelta(days=1) for zone in ('Asia/Seoul','America/New_York'))
    if any(_window(value.get('requested_window'))['end']>completed for value in state.get('refresh_windows',{}).values()):
        raise ValueError('future refresh checkpoint')
    work=[]
    if completed>=ORIGINAL_START:
        latest={'start':max(ORIGINAL_START,completed-timedelta(days=13)),'end':completed}
        key=_window_key(latest);previous=state.get('refresh_windows',{}).get(key)
        if previous is None or previous.get('status')!='request_queue_complete':work.append(('refresh',latest,previous))
        older=[value for name,value in state.get('refresh_windows',{}).items() if name!=key
            and value.get('status') not in {'request_queue_complete','source_pass_complete'}]
        if older:
            oldest=min(older,key=lambda value:value['requested_window']['end'])
            if oldest['requested_window']['end']>completed:raise ValueError('future refresh checkpoint')
            work.append(('refresh',oldest['requested_window'],oldest))
    backfill=state.get('backfill')
    if backfill is None or backfill.get('status') not in {'request_queue_complete','source_pass_complete'}:
        work.append(('backfill',None,backfill))
    return work


def krx_plan_window(scope,*,as_of,prior=None,max_calendar_days=31,completed_through=None,original_complete=False):
    """Bound planning work, retaining every original calendar date and service.

    Explicit refresh requests carry a local-day anchor and the original scope.
    Rolling fourteen-day requests and catchup jobs never replace the backfill.
    """
    _aware(as_of)
    if type(max_calendar_days) is not int or not 1<=max_calendar_days<=366:raise ValueError('bounded KRX planning batch required')
    completed=as_of.astimezone(ZoneInfo('Asia/Seoul')).date()-timedelta(days=1)
    anchor=as_of.astimezone(ZoneInfo('Asia/Seoul')).date()
    ready=provider_ready_date('krx',as_of) if original_complete else completed
    first=ORIGINAL_START
    if prior is not None:
        _binding(prior,scope)
        if prior.get('planned_through'):
            last=date.fromisoformat(prior['planned_through'])
            if not ORIGINAL_START<=last<=ORIGINAL_END:raise ValueError('KRX planned checkpoint outside original scope')
            first=last+timedelta(days=1)
    last=min(ORIGINAL_END,completed,first+timedelta(days=max_calendar_days-1))
    since=date.fromisoformat(completed_through) if completed_through else min(ORIGINAL_END,completed)
    if prior and prior.get('refresh_requests_completed_through'):
        since=max(since,date.fromisoformat(prior['refresh_requests_completed_through']))
    planned=since
    if prior and prior.get('refresh_planned_through'):planned=max(planned,date.fromisoformat(prior['refresh_planned_through']))
    if planned>completed:raise ValueError('KRX refresh checkpoint is after completed local day')
    catchup_start=max(ORIGINAL_START,planned+timedelta(days=1))
    refresh=[]
    last_ready=date.fromisoformat(prior['last_refresh_ready_date']) if prior and prior.get('last_refresh_ready_date') else None
    if last_ready is not None and last_ready>completed:raise ValueError('future KRX ready-date checkpoint')
    due=not original_complete or last_ready is None or ready>last_ready
    if due and catchup_start<=ready:
        refresh.append({'kind':'catchup','start':catchup_start,'end':min(ready,catchup_start+timedelta(days=max_calendar_days-1))})
    rolling_start=max(ORIGINAL_START,ready-timedelta(days=13))
    if due and rolling_start<=ready:refresh.append({'kind':'rolling','start':rolling_start,'end':ready})
    if original_complete:anchor=ready+timedelta(days=1)
    result={'scope':copy.deepcopy(scope),'start':first if first<=last else None,'end':last if first<=last else None,
        'services':list(KRX_SERVICES),'full_request_target':((ORIGINAL_END-ORIGINAL_START).days+1)*len(KRX_SERVICES),
        'completed_kr_day':completed,'coverage_complete':False,
        'rollforward_status':'publication_gated_completed_pass_refresh' if original_complete else 'explicit_completed_day_refresh',
        'original_request_pass_complete':original_complete,'provider_ready_date':ready,
        'refresh_anchor':anchor,'refresh_windows':refresh,
        'refresh_requests_completed_through':since,'refresh_watermark_is_coverage_claim':False}
    return result


def _krx_original_complete(engine,scope,previous):
    if not previous or previous.get('planned_through')!=ORIGINAL_END.isoformat():return False
    with transaction(engine) as session:
        jobs=session.scalars(select(CollectionJob).where(CollectionJob.source=='krx',CollectionJob.domain=='market_quotes',
            CollectionJob.request['manifest_id'].astext==scope['manifest_id'],
            ~CollectionJob.request.op('?')('refresh_anchor'))).yield_per(1000)
        return krx_original_complete(jobs,scope,planned_through=previous['planned_through'])


def _portable_summary(value):
    # Runtime timing values are diagnostic; encode their exact displayed decimal
    # text instead of admitting binary floats into the private data format.
    if isinstance(value,float):
        if not math.isfinite(value):raise ValueError('nonfinite source diagnostic')
        return Decimal(str(value))
    if isinstance(value,dict):return {k:_portable_summary(v) for k,v in value.items() if k not in {'progress_path'}}
    if isinstance(value,list):return [_portable_summary(v) for v in value]
    return value


def _krx_request_watermark(jobs,since,completed):
    success={}
    for job in jobs:
        if job.status!='succeeded':continue
        request=job.request
        service=request.get('service')
        try:day=date.fromisoformat(request.get('session_date',''))
        except ValueError:continue
        if service in KRX_SERVICES and since<day<=completed:success.setdefault(day,set()).add(service)
    result=since
    while result<completed and success.get(result+timedelta(days=1),set())==set(KRX_SERVICES):result+=timedelta(days=1)
    return result


def krx_quote_step(engine,raw_root,*,scope,previous,budget_seconds,as_of,completed_through=None,deadline=None,**_):
    _budget(budget_seconds);local_deadline=time.monotonic()+budget_seconds
    deadline=min(local_deadline,deadline) if deadline is not None else local_deadline
    original_complete=_krx_original_complete(engine,scope,previous)
    window=krx_plan_window(scope,as_of=as_of,prior=previous,completed_through=completed_through,original_complete=original_complete)
    state=copy.deepcopy(previous or {'scope':scope,'planned_through':None})
    planned=None
    if window['start'] is not None and time.monotonic()<deadline:
        with transaction(engine) as session:
            manifest=session.get(TargetUniverseManifest,scope['manifest_id'])
            if full_manifest_scope(manifest)!=scope:raise ValueError('source manifest changed during planning')
            planned=plan_krx_quotes(session,manifest,window['start'],window['end'],services=KRX_SERVICES)
        # Cursor advances only after the complete planning transaction commits.
        state['planned_through']=window['end'].isoformat()
    refresh_plans=[]
    for refresh in window['refresh_windows']:
        if time.monotonic()>=deadline:break
        with transaction(engine) as session:
            manifest=session.get(TargetUniverseManifest,scope['manifest_id'])
            if full_manifest_scope(manifest)!=scope:raise ValueError('source manifest changed during refresh')
            refresh_plans.append(plan_krx_quotes(session,manifest,refresh['start'],refresh['end'],services=KRX_SERVICES,
                refresh=True,refresh_anchor=window['refresh_anchor']))
        if refresh['kind']=='catchup':state['refresh_planned_through']=refresh['end'].isoformat()
    if len(refresh_plans)==len(window['refresh_windows']) and refresh_plans:
        state['last_refresh_ready_date']=window['provider_ready_date'].isoformat()
    remaining=deadline-time.monotonic()
    summary=run_krx_quote_collection(engine,raw_root,budget_seconds=remaining,services=KRX_SERVICES,deadline=deadline) if remaining>0 else {'status':'budget_deferred'}
    since=window['refresh_requests_completed_through']
    with transaction(engine) as session:
        jobs=session.scalars(select(CollectionJob).where(CollectionJob.source=='krx',CollectionJob.domain=='market_quotes',
            CollectionJob.status=='succeeded',CollectionJob.request['session_date'].astext>since.isoformat(),
            CollectionJob.request['session_date'].astext<=window['completed_kr_day'].isoformat())).yield_per(1000)
        watermark=_krx_request_watermark(jobs,since,window['completed_kr_day'])
    state.update(completed_kr_day=window['completed_kr_day'],rollforward_status=window['rollforward_status'],
        original_request_pass_complete=original_complete,provider_ready_date=window['provider_ready_date'],
        refresh_requests_completed_through=watermark.isoformat(),refresh_anchor=window['refresh_anchor'],
        refresh_watermark_is_coverage_claim=False,coverage_complete=False)
    outcome={'handler':'krx_quotes','status':summary['status'],'planning':planned,
        'collection':_portable_summary(summary),'refresh_planning':refresh_plans,
        'rollforward_status':window['rollforward_status'],'coverage_complete':False}
    dumps(state);dumps(outcome)
    return state,outcome


def _supplemental_bind(progress,scope,requested_window=None):
    expected={key:scope[key] for key in ('manifest_id','target_count','output_start','output_end')}
    if requested_window is not None:
        _window(requested_window)
        expected.update(output_start=requested_window['start'].isoformat(),output_end=requested_window['end'].isoformat(),
            original_output_start=scope['output_start'],original_output_end=scope['output_end'])
    if not isinstance(progress,dict) or any(progress.get(key)!=value for key,value in expected.items()):
        raise ValueError('supplemental original scope binding mismatch')


def _supplemental_cursor_path(path):
    return (len(path)==3 and path[0]=='actions' and path[2]=='next_page_token'
        or path==('trade_proof','next_page_token')
        or len(path)==4 and path[:2]==('trade_proof','pages') and type(path[2]) is int and path[3]=='page_token')


def _supplemental_cursors(value,*,runtime=False,path=()):
    if isinstance(value,dict):
        result={}
        for key,item in value.items():
            child=path+(key,)
            if key in {'page_token','next_page_token'}:
                if not _supplemental_cursor_path(child):raise ValueError('unrecognized supplemental pagination field')
                if item is None:
                    # The codec forbids private-looking untyped keys, including
                    # null cursors. Absence means no continuation on the wire.
                    if runtime:result[key]=None
                    continue
                if runtime:
                    if not isinstance(item,PaginationCursor):raise ValueError('typed supplemental pagination required')
                    result[key]=item.value
                else:result[key]=PaginationCursor(item)
            else:result[key]=_supplemental_cursors(item,runtime=runtime,path=child)
        return result
    if isinstance(value,list):return [_supplemental_cursors(item,runtime=runtime,path=path+(index,)) for index,item in enumerate(value)]
    return copy.deepcopy(value)


def portable_supplemental_progress(progress,scope,*,requested_window=None):
    _supplemental_bind(progress,scope,requested_window)
    wire={'scope':copy.deepcopy(scope),'progress':_supplemental_cursors(progress)}
    if requested_window is not None:wire['requested_window']=copy.deepcopy(requested_window)
    # Retain the presence of null cursors without bypassing the privacy codec.
    null_paths=[]
    def scan(value,path=()):
        if isinstance(value,dict):
            for key,item in value.items():
                child=path+(key,)
                if key in {'page_token','next_page_token'} and item is None:null_paths.append(list(child))
                else:scan(item,child)
        elif isinstance(value,list):
            for index,item in enumerate(value):scan(item,path+(index,))
    scan(progress)
    wire['null_cursor_paths']=null_paths
    dumps(wire)
    return wire


def runtime_supplemental_progress(wire,scope):
    _binding(wire,scope);dumps(wire)
    progress=_supplemental_cursors(wire['progress'],runtime=True)
    for path in wire.get('null_cursor_paths',[]):
        if not isinstance(path,list) or not _supplemental_cursor_path(tuple(path)):raise ValueError('invalid null pagination path')
        parent=progress
        for key in path[:-1]:parent=parent[key]
        if path[-1] in parent:raise ValueError('conflicting null pagination checkpoint')
        parent[path[-1]]=None
    _supplemental_bind(progress,scope,wire.get('requested_window'))
    return progress


def _one_supplemental_window(engine,raw_root,*,scope,previous,budget_seconds,work_root,requested_window=None):
    _budget(budget_seconds)
    from ..config import Settings
    from ..supplemental.collector import SupplementalCollector
    from ..supplemental.storage import exact_json
    filename='supplemental-backfill-progress.json' if requested_window is None else 'supplemental-refresh-'+_window_key(requested_window)+'-progress.json'
    path=Path(work_root)/'source-handlers'/filename
    path.parent.mkdir(parents=True,exist_ok=True)
    if previous is not None:
        restored=runtime_supplemental_progress(previous,scope)
        path.write_text(json.dumps(exact_json(restored),ensure_ascii=False),encoding='utf-8')
    elif path.exists():raise ValueError('fresh supplemental checkpoint path required')
    collector=SupplementalCollector(engine,Settings('',Path(raw_root)),path,manifest_id=scope['manifest_id'],
        requested_window=requested_window,min_free_bytes=2*1024**3)
    try:
        summary=collector.run(budget_seconds)
        # Use the last durable checkpoint emitted after source SQL commits,
        # with the same Decimal/date representation as native collector resume.
        state=portable_supplemental_progress(json.loads(path.read_text(encoding='utf-8')),scope,requested_window=requested_window)
    finally:
        for iterator in collector.nport_iterators.values():
            close=getattr(iterator,'close',None)
            if close:close()
        collector.http.close()
    return state,{'handler':'supplemental','status':summary['status'],'collection':_portable_summary(summary),
        'requested_window':requested_window,'runtime_disk_guard_bytes':2*1024**3,
        'public_catalog_refresh_complete':False,'historical_population_complete':False,'coverage_complete':False}


def supplemental_step(engine,raw_root,*,scope,previous,budget_seconds,work_root,as_of=None,**_):
    _budget(budget_seconds);state=_window_state(previous,scope);work=refresh_work_windows(state,as_of=as_of or utcnow())
    deadline=time.monotonic()+budget_seconds;outcomes=[]
    for position,(kind,window,prior) in enumerate(work):
        remaining=deadline-time.monotonic()
        if remaining<=0:break
        try:
            updated,outcome=_one_supplemental_window(engine,raw_root,scope=scope,previous=prior,budget_seconds=remaining/(len(work)-position),
                work_root=work_root,requested_window=window)
        except Exception as exc:
            filename='supplemental-backfill-progress.json' if window is None else 'supplemental-refresh-'+_window_key(window)+'-progress.json'
            path=Path(work_root)/'source-handlers'/filename;updated=prior
            try:
                if path.exists():updated=portable_supplemental_progress(json.loads(path.read_text(encoding='utf-8')),scope,requested_window=window)
            except Exception:pass
            outcome={'handler':'supplemental','status':'source_error','reason':type(exc).__name__,'requested_window':window,'coverage_complete':False}
        if updated is not None:
            if kind=='backfill':state['backfill']=updated
            else:state['refresh_windows'][_window_key(window)]=updated
        outcomes.append(outcome)
    dumps(state)
    return state,{'handler':'supplemental','status':'partial' if any(item['status']!='source_error' for item in outcomes) else 'source_error',
        'window_outcomes':outcomes,'rollforward_status':'explicit_completed_shared_day_refresh','coverage_complete':False}


def _recover_committed_state(name,engine,scope,work_root,previous,adjustment):
    """Recovery is read-only and can only retain a validated bound checkpoint."""
    try:
        if name=='supplier_adjusted':
            recovered=_window_state(previous,scope)
            with transaction(engine) as session:
                row=session.scalar(select(SupplierPriceCollectionState).where(SupplierPriceCollectionState.manifest_id==scope['manifest_id'],
                    SupplierPriceCollectionState.source=='alpaca',SupplierPriceCollectionState.adjustment==adjustment))
                if row is not None:recovered['backfill']=portable_supplier_state(row,scope)
                for refresh in session.scalars(select(SupplierPriceRefreshState).where(SupplierPriceRefreshState.manifest_id==scope['manifest_id'],
                        SupplierPriceRefreshState.source=='alpaca',SupplierPriceRefreshState.adjustment==adjustment)).yield_per(100):
                    window={'start':refresh.output_start,'end':refresh.output_end}
                    recovered['refresh_windows'][_window_key(window)]=portable_supplier_state(refresh,scope,requested_window=window)
            if recovered['backfill'] is not None or recovered['refresh_windows']:return recovered
        elif name=='supplemental':
            recovered=_window_state(previous,scope);root=Path(work_root)/'source-handlers'
            for filename in ('supplemental-progress.json','supplemental-backfill-progress.json'):
                path=root/filename
                if path.exists():recovered['backfill']=portable_supplemental_progress(json.loads(path.read_text(encoding='utf-8')),scope)
            for path in root.glob('supplemental-refresh-*-progress.json'):
                match=re.fullmatch(r'supplemental-refresh-(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})-progress\.json',path.name)
                if not match:continue
                window={'start':date.fromisoformat(match[1]),'end':date.fromisoformat(match[2])}
                recovered['refresh_windows'][_window_key(window)]=portable_supplemental_progress(json.loads(path.read_text(encoding='utf-8')),scope,requested_window=window)
            if recovered['backfill'] is not None or recovered['refresh_windows']:return recovered
    except Exception:
        # An unsafe or unreadable checkpoint cannot replace the prior proof of
        # committed traversal. No exception text is exposed to private bundles.
        pass
    return previous


def _supplier_adjustment(value):
    definition=classify_price_basis('alpaca','/v2/stocks/DECLARED/bars',{'adjustment':value})
    if definition['basis']!='provider_adjusted':raise ValueError('explicit supplier adjusted basis required')


def _supplier_cursor(cursor,scope,*,runtime=False):
    if not isinstance(cursor,dict) or set(cursor)-SUPPLIER_CURSOR_FIELDS:raise ValueError('nonportable supplier operational field')
    result=copy.deepcopy(cursor)
    for key in SUPPLIER_CURSOR_FIELDS-{'page_token','request_anchor'}:
        value=result.get(key,0)
        if type(value) is not int or value<0:raise ValueError('exact nonnegative supplier cursor required')
    index=result.get('member_index',0)
    if index>scope['market_target_counts']['US']:raise ValueError('supplier member cursor outside whole source target')
    token=result.get('page_token')
    if token is not None:
        if runtime:
            if not isinstance(token,PaginationCursor):raise ValueError('typed supplier pagination required')
            result['page_token']=token.value
        else:result['page_token']=PaginationCursor(token)
        anchor=result.get('request_anchor')
        if not anchor or index>=scope['market_target_counts']['US']:raise ValueError('supplier continuation anchor required')
    elif runtime:result['page_token']=None
    else:result.pop('page_token',None)
    if result.get('request_anchor') is not None:_aware(datetime.fromisoformat(result['request_anchor']))
    return result


def portable_supplier_state(state,scope,*,requested_window=None):
    start,end=ORIGINAL_START,ORIGINAL_END
    if requested_window is not None:
        _window(requested_window);start,end=requested_window['start'],requested_window['end']
    if (state.manifest_id,state.target_hash,state.target_count,state.market_target_count,state.output_start,state.output_end)!=(
            scope['manifest_id'],scope['target_hash'],17758,13251,start,end):
        raise ValueError('supplier original scope binding mismatch')
    _supplier_adjustment(state.adjustment)
    if state.source!='alpaca' or state.status not in {'ready','collecting','budget_deferred','blocked','request_queue_complete'}:
        raise ValueError('invalid supplier checkpoint source/status')
    result={'scope':copy.deepcopy(scope),'source':'alpaca','adjustment':state.adjustment,'status':state.status,
        'cursor':_supplier_cursor(state.cursor,scope),'updated_at':_aware(state.updated_at),'last_reason':state.last_reason,
        'coverage_complete':False,'rollforward_status':'explicit_completed_day_refresh' if requested_window is not None else 'original_backfill'}
    if requested_window is not None:result['requested_window']=copy.deepcopy(requested_window)
    dumps(result)
    return result


def _supplier_runtime_values(portable,scope):
    _binding(portable,scope);dumps(portable)
    required={'scope','source','adjustment','status','cursor','updated_at','last_reason','coverage_complete','rollforward_status'}
    requested=portable.get('requested_window')
    if requested is not None:required.add('requested_window');_window(requested)
    if set(portable)!=required or portable['source']!='alpaca' or portable['coverage_complete'] is not False:
        raise ValueError('invalid portable supplier state')
    _supplier_adjustment(portable['adjustment'])
    cursor=_supplier_cursor(portable['cursor'],scope,runtime=True)
    if portable['status'] not in {'ready','collecting','budget_deferred','blocked','request_queue_complete'}:
        raise ValueError('invalid supplier status')
    model=SupplierPriceRefreshState if requested is not None else SupplierPriceCollectionState
    values=dict(manifest_id=scope['manifest_id'],source='alpaca',adjustment=portable['adjustment'],
        target_hash=scope['target_hash'],target_count=17758,market_target_count=13251,
        output_start=requested['start'] if requested else ORIGINAL_START,output_end=requested['end'] if requested else ORIGINAL_END,
        status=portable['status'],cursor=cursor,updated_at=_aware(portable['updated_at']),last_reason=portable['last_reason'])
    return model,values


def restore_supplier_state(session,portable,scope,*,authority):
    _authority(authority);model,values=_supplier_runtime_values(portable,scope)
    if session.scalar(select(model.id).limit(1)) is not None:
        raise ValueError('supplier restore requires an empty fresh operational table')
    restored=model(**values)
    session.add(restored);session.flush()
    return restored


def supplier_adjusted_step(engine,raw_root,*,scope,previous,budget_seconds,adjustment='split',authority=AUTHORITY,as_of=None,**_):
    _authority(authority);_budget(budget_seconds);_supplier_adjustment(adjustment)
    state=_window_state(previous,scope);work=refresh_work_windows(state,as_of=as_of or utcnow())
    portable=[value for value in [state.get('backfill'),*state['refresh_windows'].values()] if value is not None]
    restored=[]
    for value in portable:
        if value.get('adjustment')!=adjustment:raise ValueError('supplier requested adjustment changed')
        restored.append(_supplier_runtime_values(value,scope))
    with transaction(engine) as session:
        # One atomic admission check covers every dated refresh checkpoint.
        # Existing local or remotely active state is never overwritten.
        if any(session.scalar(select(model.id).limit(1)) is not None for model in (SupplierPriceCollectionState,SupplierPriceRefreshState)):
            raise ValueError('supplier collection requires empty fresh operational tables')
        for model,values in restored:session.add(model(**values))
        session.flush()
    from ..credentials import load_credentials
    from ..ingestion.http import ProviderHTTP
    http=ProviderHTTP(engine);credentials=load_credentials(sources=['alpaca']);deadline=time.monotonic()+budget_seconds;outcomes=[]
    try:
        for position,(kind,window,prior) in enumerate(work):
            remaining=deadline-time.monotonic()
            if remaining<=0:break
            try:
                summary=collect_supplier_adjusted(engine,http,credentials,raw_root,manifest_id=scope['manifest_id'],adjustment=adjustment,
                    budget_seconds=remaining/(len(work)-position),max_pages=50,
                    requested_window=(window['start'],window['end']) if window is not None else None)
                outcome={'handler':'supplier_adjusted','status':summary['status'],'collection':_portable_summary(summary),
                    'requested_window':window,'coverage_complete':False}
            except Exception as exc:
                outcome={'handler':'supplier_adjusted','status':'source_error','reason':type(exc).__name__,
                    'requested_window':window,'coverage_complete':False}
            model=SupplierPriceRefreshState if window is not None else SupplierPriceCollectionState
            start,end=(window['start'],window['end']) if window is not None else (ORIGINAL_START,ORIGINAL_END)
            with transaction(engine) as session:
                current=session.scalar(select(model).where(model.manifest_id==scope['manifest_id'],model.source=='alpaca',
                    model.adjustment==adjustment,model.output_start==start,model.output_end==end))
                if current is not None:
                    updated=portable_supplier_state(current,scope,requested_window=window)
                    if kind=='backfill':state['backfill']=updated
                    else:state['refresh_windows'][_window_key(window)]=updated
            outcomes.append(outcome)
    finally:http.close()
    dumps(state)
    return state,{'handler':'supplier_adjusted','status':'partial' if any(item['status']!='source_error' for item in outcomes) else 'source_error',
        'window_outcomes':outcomes,'rollforward_status':'explicit_completed_shared_day_refresh','coverage_complete':False}


def _owned_handler_specs(slots,owned_sources):
    specs=[];sources={slot.get('source') for slot in slots}
    for slot in slots:
        if slot.get('owner')!='remote' or slot.get('local_enabled') is not False or not slot.get('account_id'):
            raise ValueError('local account acquisition must be disabled before remote handlers')
        source=slot.get('source')
        if source not in owned_sources:raise ValueError('handler source outside explicit ownership')
        name='krx_quotes' if source=='krx' else 'supplemental' if source=='supplemental' else 'supplier_adjusted' if source=='alpaca' and slot.get('handler')=='supplier_adjusted' else None
        if name is None:continue
        if name=='krx_quotes' and (slot.get('market') not in {'kr','both'} or slot.get('domain')!='prices'):
            raise ValueError('KRX handler requires Korean price ownership')
        if name=='supplemental' and (slot.get('market')!='both' or slot.get('domain')!='fundamentals'
                or not SUPPLEMENTAL_DEPENDENCIES.issubset(set(owned_sources)&sources)):
            raise ValueError('supplemental credential dependencies require explicit remote ownership')
        if name=='supplier_adjusted' and (slot.get('domain')!='prices' or slot.get('market') not in {'us','both'}):
            raise ValueError('supplier adjusted handler requires US price ownership')
        if any(item[0]==name for item in specs):raise ValueError('duplicate source handler assignment')
        specs.append((name,slot))
    return specs


def run_owned_handlers(engine,raw_root,*,manifest_id,slots,owned_sources,checkpoint,budget_seconds,work_root,
                       authority=AUTHORITY,as_of=None,deadline=None):
    """Worker hook returning only safe state and bounded explicit outcomes."""
    _authority(authority);_budget(budget_seconds)
    local_deadline=time.monotonic()+budget_seconds
    deadline=min(local_deadline,deadline) if deadline is not None else local_deadline
    specs=_owned_handler_specs(slots,set(owned_sources))
    previous=checkpoint.get('source_handler_state',{})
    if not isinstance(previous,dict) or set(previous)-HANDLERS:raise ValueError('unknown source handler checkpoint')
    dumps(previous)
    if not specs:return {'source_handler_state':copy.deepcopy(previous),'source_handler_outcomes':[],'coverage_complete':False}
    with transaction(engine) as session:
        scope=full_manifest_scope(session.get(TargetUniverseManifest,manifest_id))
    for value in previous.values():_binding(value,scope)
    as_of=_aware(as_of or utcnow())
    state=copy.deepcopy(previous);outcomes=[]
    for position,(name,slot) in enumerate(specs):
        if name in state:_binding(state[name],scope)
        remaining=deadline-time.monotonic()
        if remaining<=0:
            outcomes.append({'handler':name,'status':'budget_deferred','coverage_complete':False});continue
        share=remaining/(len(specs)-position)
        function={'krx_quotes':krx_quote_step,'supplemental':supplemental_step,'supplier_adjusted':supplier_adjusted_step}[name]
        try:
            updated,outcome=function(engine,raw_root,scope=scope,previous=state.get(name),budget_seconds=share,
                as_of=as_of,work_root=work_root,authority=authority,adjustment=slot.get('adjustment','split'),
                deadline=deadline,
                completed_through=checkpoint.get('completed_through',{}).get('krx'))
            dumps(updated);dumps(outcome)
        except Exception as exc:
            updated=_recover_committed_state(name,engine,scope,work_root,state.get(name),slot.get('adjustment','split'))
            sqlstate=getattr(getattr(exc,'orig',None),'sqlstate',None)
            temporary=bool(sqlstate and (sqlstate.startswith('08') or sqlstate in {'57P01','57P02','57P03'})) or sqlstate=='57014' and 'statement timeout' in str(getattr(exc,'orig','')).lower()
            outcome={'handler':name,'status':'source_error','reason':type(exc).__name__,'coverage_complete':False,
                'checkpoint_recovered':updated is not None,'retryable':temporary}
        if updated is not None:state[name]=updated
        outcomes.append(outcome)
    result={'source_handler_state':state,'source_handler_outcomes':outcomes,'coverage_complete':False}
    dumps(result)
    return result
