"""Separate adjusted-bar traversal with page data and checkpoint committed together."""
from dataclasses import dataclass
from datetime import date,datetime
import json
from pathlib import Path
import time
from sqlalchemy import select,text
from sqlalchemy.orm import Session
from .adapters.alpaca import AlpacaDailyRequest
from .provider_basis import classify_price_basis,supplier_daily_visible
from .supplier_storage import store_supplier_prices
from .supplier_models import SupplierPriceCollectionState
from .supplier_refresh_models import SupplierPriceRefreshState
from ..ingestion.models import TargetUniverseManifest
from ..providers import AlpacaData
from ..ingestion.http import ProviderError
from ..db import utcnow
from ..releases.models import ValidationIssue

def record_supplier_unavailable(session,state,member,request,code):
    """An unsupported symbol is a gap, never a reason to shrink the universe."""
    if str(code)!='404':return False
    cursor=dict(state.cursor);cursor.update(member_index=cursor.get('member_index',0)+1,page_token=None,request_anchor=None,
        completed_members=cursor.get('completed_members',0)+1,unsupported_members=cursor.get('unsupported_members',0)+1)
    state.cursor=cursor;state.status='collecting';state.updated_at=utcnow()
    session.add(ValidationIssue(record_domain='price_observation',record_id=None,rule='supplier_symbol_unavailable',severity='gap',
        evidence={'security_id':member['security_id'],'source':'alpaca','basis':'provider_adjusted','adjustment':request.adjustment,
            'output_start':request.start.isoformat(),'output_end':request.end.isoformat(),'provider_code':'404','coverage_complete':False}))
    return True

@dataclass(frozen=True)
class SupplierAlpacaRequest:
    start:date
    end:date
    symbol:str
    adjustment:str
    page_token:str|None
    captured_at:datetime

    def __post_init__(self):
        definition=classify_price_basis('alpaca','/v2/stocks/{symbol}/bars',{'adjustment':self.adjustment})
        if definition['basis']!='provider_adjusted':raise ValueError('Adjusted traversal required')
        self.raw_request()

    def raw_request(self):return AlpacaDailyRequest(self.start,self.end,symbol=self.symbol,page_token=self.page_token,captured_at=self.captured_at,page_size=100)
    @property
    def effective_end(self):return self.raw_request().effective_end
    def endpoint(self):return self.raw_request().endpoint()
    def params(self):return {**self.raw_request().params(),'adjustment':self.adjustment}

def commit_supplier_page(session,state,*,request,response,payload,raw_root,member):
    """Do not expose the next token until all page rows have committed."""
    if 'next_page_token' not in payload:raise ValueError('Explicit supplier continuation field required')
    result=store_supplier_prices(session,security_id=member['security_id'],source='alpaca',market='US',
        endpoint=request.endpoint(),params=request.params(),response=response,payload=payload,raw_root=raw_root,
        output_start=request.start,output_end=request.end)
    token=payload.get('next_page_token')
    if token is not None and (not isinstance(token,str) or not token or len(token)>8192):raise ValueError('Invalid supplier continuation')
    if token is not None and token==request.page_token:raise ValueError('Repeated supplier continuation')
    cursor=dict(state.cursor);cursor.update(page_token=token,pages=cursor.get('pages',0)+1,
        inserted=cursor.get('inserted',0)+result['inserted'],source_rows=cursor.get('source_rows',0)+result['source_rows'],
        rejected=cursor.get('rejected',0)+result['rejected'])
    if token is None:
        cursor.update(member_index=cursor.get('member_index',0)+1,page_token=None,request_anchor=None,
            completed_members=cursor.get('completed_members',0)+1,
            empty_members=cursor.get('empty_members',0)+(result['source_rows']==0))
    state.cursor=cursor;state.updated_at=utcnow();state.status='collecting'
    session.flush()
    return result

def collect_supplier_adjusted(engine,http,credentials,raw_root,*,manifest_id,adjustment='split',
                             budget_seconds=600,max_pages=None,progress_path=None,requested_window=None):
    provider=AlpacaData(http,credentials);started=time.monotonic();attempted=0
    state_model=SupplierPriceRefreshState if requested_window is not None else SupplierPriceCollectionState
    if requested_window is not None:
        if (not isinstance(requested_window,tuple) or len(requested_window)!=2
            or any(type(d) is not date for d in requested_window) or requested_window[0]>requested_window[1]
            or not supplier_daily_visible(requested_window[1],'US',utcnow())):
            raise ValueError('Explicit completed US refresh window required')
    lock_key=f'supplier_prices:alpaca:{adjustment}:{manifest_id}'+(':refresh' if requested_window is not None else '')
    with engine.connect() as lock:
        owned=lock.execute(text('SELECT pg_try_advisory_lock(hashtext(:key))'),{'key':lock_key}).scalar_one();lock.commit()
        if not owned:return {'status':'already_running','coverage_complete':False}
        try:
            with Session(engine) as session,session.begin():
                manifest=session.get(TargetUniverseManifest,manifest_id)
                if not manifest:raise ValueError('Original target missing')
                members=[dict(m) for m in manifest.members if m['market']=='US']
                start=date.fromisoformat(manifest.scope['output_start']);end=date.fromisoformat(manifest.scope['output_end'])
                if requested_window is not None:
                    if requested_window[0]<start:raise ValueError('Refresh cannot remove original lower boundary')
                    start,end=requested_window
                target_count=len(manifest.members);target_hash=manifest.content_hash
                state=session.scalar(select(state_model).where(state_model.manifest_id==manifest_id,
                    state_model.source=='alpaca',state_model.adjustment==adjustment,state_model.output_start==start,state_model.output_end==end).with_for_update())
                if state is None:
                    state=state_model(manifest_id=manifest_id,source='alpaca',adjustment=adjustment,
                        target_hash=target_hash,target_count=target_count,market_target_count=len(members),
                        output_start=start,output_end=end,status='ready',cursor={'member_index':0,'pages':0,'inserted':0})
                    session.add(state);session.flush()
                if (state.target_hash,state.target_count,state.output_start,state.output_end)!=(target_hash,target_count,start,end):raise ValueError('Supplier target changed')
                state_id=state.id
            while time.monotonic()-started<budget_seconds and (max_pages is None or attempted<max_pages):
                with Session(engine) as session,session.begin():
                    state=session.get(state_model,state_id);cursor=dict(state.cursor);index=cursor['member_index']
                    if index>=len(members):state.status='request_queue_complete';state.updated_at=utcnow();break
                    anchor=datetime.fromisoformat(cursor['request_anchor']) if cursor.get('request_anchor') else utcnow()
                    cursor['request_anchor']=anchor.isoformat();state.cursor=cursor
                member=members[index]
                request=SupplierAlpacaRequest(start,end,member['symbol'],adjustment,cursor.get('page_token'),anchor)
                try:response,payload=provider.daily_prices(request)
                except ProviderError as exc:
                    with Session(engine) as session,session.begin():
                        state=session.get(state_model,state_id)
                        skipped=record_supplier_unavailable(session,state,member,request,exc.code)
                        if not skipped:state.status='blocked';state.last_reason=exc.code;state.updated_at=utcnow()
                    if skipped:
                        attempted+=1
                        if progress_path:write_supplier_progress(engine,state_id,progress_path,state_model=state_model)
                        continue
                    break
                with Session(engine) as session,session.begin():
                    state=session.get(state_model,state_id)
                    commit_supplier_page(session,state,request=request,response=response,payload=payload,raw_root=raw_root,member=member)
                attempted+=1
                if progress_path:write_supplier_progress(engine,state_id,progress_path,state_model=state_model)
            with Session(engine) as session,session.begin():
                state=session.get(state_model,state_id)
                if state.cursor.get('member_index',0)>=len(members):state.status='request_queue_complete'
                elif state.status=='collecting':state.status='budget_deferred'
            return write_supplier_progress(engine,state_id,progress_path,state_model=state_model)
        finally:
            lock.execute(text('SELECT pg_advisory_unlock(hashtext(:key))'),{'key':lock_key});lock.commit()

def write_supplier_progress(engine,state_id,path=None,*,state_model=SupplierPriceCollectionState):
    with Session(engine) as session:
        state=session.get(state_model,state_id)
        value={'status':state.status,'source':state.source,'adjustment':state.adjustment,'state_id':state.id,
            'original_target_count':state.target_count,'market_target_count':state.market_target_count,
            'output_start':state.output_start.isoformat(),'output_end':state.output_end.isoformat(),
            'checked_at':utcnow().isoformat(),'last_commit_at':state.updated_at.isoformat(),'reason':state.last_reason,
            'progress':{k:v for k,v in state.cursor.items() if k not in {'page_token','request_anchor'}},
            'basis':'provider_adjusted','evidence_grade':'supplier_declared','corporate_action_role':'supplemental',
            'historical_adjustment_reference_verified':False,'coverage_complete':False}
    if path:
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);pending=path.with_suffix('.tmp')
        pending.write_text(json.dumps(value,indent=2),encoding='utf-8');pending.replace(path)
    return value
