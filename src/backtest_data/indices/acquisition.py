"""Bounded independent official index acquisition; never starts other collectors."""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
import json
import re
import time
from urllib.parse import urljoin, urlsplit, parse_qs
import httpx
from sqlalchemy import exists, select, text
from sqlalchemy.orm import Session
from ..db import new_id, utcnow
from ..ingestion.jobs import stable_hash, reserve_rate_slot, RateReservationDeferred
from ..ingestion.models import TargetUniverseManifest
from ..supplemental.models import SupplementalDocument, SupplementalObservation
from ..supplemental.storage import register_document
from .models import IndexAcquisitionState
from .requests import IndexRequest, START, END, default_requests, lseg_catalog_request, nasdaq_catalog_request, original_scope
from .parsers import html_text, pdf_pages, parse_lseg_catalog, parse_nasdaq_notice, parse_russell_pages, parse_russell_ipo_pages
from .storage import store_records

@dataclass(frozen=True)
class IndexResponse:
    content: bytes
    status_code: int
    captured_at: datetime
    content_type: str | None = None

class PublicIndexClient:
    """Unauthenticated public transport, no redirects or credential/config loading."""
    def __init__(self,*,engine=None,max_bytes=8*1024*1024,transport=None):
        if not 1 <= max_bytes <= 32*1024*1024: raise ValueError('Bounded response envelope required')
        self.max_bytes=max_bytes
        self.engine=engine
        self.client=httpx.Client(follow_redirects=False,transport=transport,headers={'User-Agent':'BacktestData official-public-index-history/1.0','Accept':'application/json,text/html,application/pdf'})
    def __enter__(self): return self
    def __exit__(self,*args): self.client.close()
    def fetch(self,request,*,timeout_seconds):
        # Restore validation also protects durable checkpoint requests.
        request=IndexRequest.restore(request.serializable())
        deadline=time.monotonic()+timeout_seconds
        if self.engine is not None:
            with Session(self.engine) as session, session.begin():
                slot=reserve_rate_slot(session,f'{request.source}:shared_account',utcnow(),1.0,max_delay_seconds=max(0,timeout_seconds-0.1))
            delay=(slot-utcnow()).total_seconds()
            if delay>0:time.sleep(delay)
        remaining=deadline-time.monotonic()
        if remaining<=0:raise RateReservationDeferred('Source budget exhausted')
        with self.client.stream(request.method,request.url,params=dict(request.params),json=request.payload(),timeout=max(0.1,min(20,remaining))) as response:
            chunks=[];count=0
            for chunk in response.iter_bytes():
                if time.monotonic()>deadline:raise TimeoutError('Official response deadline exceeded')
                count += len(chunk)
                if count > self.max_bytes: raise ValueError('Official response byte limit exceeded')
                chunks.append(chunk)
            return IndexResponse(b''.join(chunks),response.status_code,utcnow(),response.headers.get('content-type'))

class _Links(HTMLParser):
    def __init__(self): super().__init__();self.links=[];self.current=None
    def handle_starttag(self,tag,attrs):
        if tag=='a': self.current={'attrs':dict(attrs),'parts':[]}
    def handle_data(self,value):
        if self.current is not None: self.current['parts'].append(value)
    def handle_endtag(self,tag):
        if tag=='a' and self.current is not None:
            self.links.append((self.current['attrs'],' '.join(self.current['parts']).strip()));self.current=None

def _in_scope(row):
    public=row.get('published_date') or row.get('announced_date')
    if public is not None: return START <= public <= END
    return any(START <= row[name] <= END for name in ('snapshot_date','effective_date') if row.get(name) is not None)

def parse_response(request,content):
    """Return source-only facts and explicit follow-up requests; no SQL or network."""
    context=dict(request.context)
    if request.parser=='lseg_catalog':
        from decimal import Decimal
        payload=json.loads(content,parse_float=Decimal,parse_int=Decimal)
        rows,total=parse_lseg_catalog(payload)
        body=request.payload();page=body['page_number'];size=body['page_size']
        if len(rows)!=min(size,max(0,total-page*size)): raise ValueError('Catalog page incomplete or count inconsistent')
        following=[lseg_catalog_request(body['year'],page+1,size)] if (page+1)*size<total else []
        return [row for row in rows if _in_scope(row)],following
    if request.parser=='russell_pdf':
        pages=pdf_pages(content)
        source_name={'FTSE_RUSSELL:RUSSELL_3000':'3000','FTSE_RUSSELL:RUSSELL_MICROCAP':'Microcap'}.get(context.get('index_key'))
        action_name={'add':'Additions','delete':'Deletions'}.get(context.get('action'))
        if not source_name or not action_name or not re.search(r'Russell\s+'+source_name+r'\W*Index\s*[-–]\s*'+action_name,'\n'.join(pages),re.I):
            raise ValueError('Downloaded index identity/action differs from declared resource')
        rows=parse_russell_pages(pages,index_key=context['index_key'],action=context['action'],
            effective_date=date.fromisoformat(context['effective_date']),effective_phase=context['effective_phase'],
            published_date=date.fromisoformat(context['published_date']) if context.get('published_date') else None)
        for row in rows:
            row['fields']['date_evidence_url']=context.get('date_evidence_url')
            row['fields']['effective_date_basis']='official_reconstitution_schedule_context'
        return [row for row in rows if _in_scope(row)],[]
    if request.parser in {'nasdaq_html','nasdaq_pdf'}:
        source='\n'.join(pdf_pages(content)) if request.parser=='nasdaq_pdf' else html_text(content)
        return [row for row in parse_nasdaq_notice(source) if _in_scope(row)],[]
    if request.parser=='russell_ipo_pdf':
        return [row for row in parse_russell_ipo_pages(pdf_pages(content,layout=True)) if _in_scope(row)],[]
    if request.parser=='nasdaq_catalog':
        parser=_Links();parser.feed(content.decode('utf-8-sig'))
        following=[];seen=set();next_page=None
        for attrs,label in parser.links:
            url=urljoin(request.url,attrs.get('href',''));parts=urlsplit(url)
            if parts.scheme!='https' or parts.hostname!='ir.nasdaq.com': continue
            if 'Nasdaq-100' in label and ('/news-releases/news-release-details/' in parts.path or parts.path.startswith('/node/')):
                clean=f'https://ir.nasdaq.com{parts.path}'
                if clean not in seen: following.append(IndexRequest('nasdaq_index',clean,'nasdaq_html'));seen.add(clean)
            if parts.path=='/search' and (attrs.get('rel')=='next' or label.lower().strip() in {'next','next ›','›'}):
                page=parse_qs(parts.query).get('page',[None])[0]
                if page is not None and str(page).isdigit(): next_page=int(page)
        current=int(context['page'])
        if next_page is not None:
            if next_page != current+1: raise ValueError('Noncontiguous official catalog pagination')
            following.append(nasdaq_catalog_request(int(context['year']),next_page))
        if not parser.links: raise ValueError('Official catalog page has no recognizable links')
        return [],following
    raise ValueError('No approved official index parser for request')

def supplemental_record(source):
    if source.record_kind=='index_announcement':
        return dict(record_kind='announcement',index_key='FTSE_RUSSELL:FAMILY:'+source.entity_key,row_key=source.row_key,
            announced_date=source.announced_date,published_date=source.published_date,effective_date=source.effective_date,
            fields={'source_fields':source.fields,'source_stage_kind':source.record_kind},issues=list(source.issues)+['index_identity_unresolved','baseline_unavailable'])
    mapping={'S&P 500':'SPDJI:SP500','S&P MidCap 400':'SPDJI:SP400','S&P SmallCap 600':'SPDJI:SP600'}
    if source.record_kind!='index_member' or source.entity_key not in mapping: raise ValueError('Unrecognized official supplemental index record')
    action={'ADDED':'add','DELETED':'delete'}.get(source.fields.get('action'))
    if action is None: raise ValueError('Source change action missing')
    return dict(record_kind='change',index_key=mapping[source.entity_key],row_key=source.row_key,
        member_identifier=source.member_identifier,member_name=source.member_name,action=action,
        announced_date=source.announced_date,published_date=source.published_date,effective_date=source.effective_date,
        effective_phase=source.fields.get('effective_phase','unknown'),
        fields={'source_fields':source.fields,'source_stage_kind':source.record_kind},
        issues=list(source.issues)+['historical_security_identity_unresolved','baseline_unavailable'])

def _manifest(session,manifest_id):
    manifest=session.get(TargetUniverseManifest,manifest_id)
    if manifest is None or len(manifest.members)!=17758 or manifest.scope.get('output_start')!=START.isoformat() or manifest.scope.get('output_end')!=END.isoformat():
        raise ValueError('Original full target manifest and window required')
    counts={market:sum(member.get('market')==market for member in manifest.members) for market in ('KR','US')}
    if counts!={'KR':4507,'US':13251}: raise ValueError('Original KR/US denominators required')
    return manifest

def _gap(request,*,status,reason):
    context=dict(request.context)
    return dict(record_kind='access_gap',index_key=context.get('index_key',f'OFFICIAL_SOURCE:{request.source}'),
        row_key=f'{request.parser}:{stable_hash(request.serializable())}',
        fields={'http_status':status,'reason':reason,'parser':request.parser,'original_scope':original_scope()},
        issues=[reason,'baseline_unavailable','weight_unavailable','coverage_incomplete'])

def _new_state(manifest,job_key,cursor):
    return IndexAcquisitionState(id=new_id(),manifest_id=manifest.id,job_key=job_key,target_hash=manifest.content_hash,
        target_count=17758,output_start=START,output_end=END,cursor=cursor,status='collecting',updated_at=utcnow())

def _state_matches(state,manifest):
    if state.target_hash!=manifest.content_hash or state.target_count!=17758 or state.output_start!=START or state.output_end!=END:
        raise ValueError('Index acquisition checkpoint scope mismatch')

def collect_index_history(engine,raw_root,*,manifest_id,max_requests=5,budget_seconds=30,client=None,requests=None,job_key='official_history_v1'):
    """Atomic bounded source pages. Caller explicitly invokes; no production defaults."""
    if not 1 <= max_requests <= 100 or not 0 < budget_seconds <= 3600 or not job_key or len(job_key)>80:
        raise ValueError('Bounded acquisition budget required')
    plan=list(default_requests() if requests is None else requests)
    if not plan or len(plan)>10000 or any(not isinstance(req,IndexRequest) for req in plan): raise ValueError('Explicit bounded official request plan required')
    plan_hash=stable_hash([req.serializable() for req in plan]);deadline=time.monotonic()+budget_seconds
    progress={'requests':0,'source_rows':0,'inserted':0,'access_gaps':0}
    own=client is None;api=PublicIndexClient(engine=engine) if own else client
    lock=f'official_index_history:{manifest_id}:{job_key}'
    try:
        with engine.connect() as connection:
            locked=connection.execute(text('SELECT pg_try_advisory_lock(hashtext(:key))'),{'key':lock}).scalar_one();connection.commit()
            if not locked: return {'status':'ownership_busy','coverage_complete':False,'progress':progress}
            try:
                with Session(connection) as session:
                    with session.begin():
                        manifest=_manifest(session,manifest_id)
                        state=session.scalar(select(IndexAcquisitionState).where(IndexAcquisitionState.manifest_id==manifest_id,IndexAcquisitionState.job_key==job_key))
                        if state is None:
                            state=_new_state(manifest,job_key,{'version':1,'plan_hash':plan_hash,'queue':[req.serializable() for req in plan],'completed':[],'blocked_hosts':[],'gaps':[]})
                            session.add(state);session.flush()
                        _state_matches(state,manifest)
                        if state.cursor.get('plan_hash')!=plan_hash: raise ValueError('Index acquisition request plan changed')
                        state_id=state.id;cursor=dict(state.cursor)
                    while cursor['queue'] and progress['requests']<max_requests and time.monotonic()<deadline-0.1:
                        request=IndexRequest.restore(cursor['queue'][0]);host=urlsplit(request.url).hostname
                        if host in cursor.get('blocked_hosts',[]):
                            next_cursor={**cursor,'queue':cursor['queue'][1:],'completed':cursor['completed']+[stable_hash(request.serializable())]}
                            with session.begin():
                                state=session.get(IndexAcquisitionState,state_id);state.cursor=next_cursor;state.updated_at=utcnow()
                            cursor=next_cursor;continue
                        try: response=api.fetch(request,timeout_seconds=max(0.1,deadline-time.monotonic()))
                        except Exception as exc:
                            status='budget_deferred' if isinstance(exc,RateReservationDeferred) else 'request_error'
                            with session.begin():
                                state=session.get(IndexAcquisitionState,state_id);state.status=status;state.cursor={**cursor,'last_error':type(exc).__name__};state.updated_at=utcnow()
                            return {'status':status,'reason':type(exc).__name__,'coverage_complete':False,'progress':progress}
                        progress['requests']+=1
                        following=[];reason=None
                        if response.status_code!=200:
                            reason='access_denied' if response.status_code in {401,403} else f'http_status_{response.status_code}'
                            rows=[_gap(request,status=response.status_code,reason=reason)]
                        else:
                            try: rows,following=parse_response(request,response.content)
                            except Exception as exc:
                                reason='parser_unavailable' if isinstance(exc,RuntimeError) else 'unrecognized_official_schema'
                                rows=[_gap(request,status=200,reason=reason)]
                        completed=cursor['completed']+[stable_hash(request.serializable())]
                        queue=cursor['queue'][1:];existing={stable_hash(req) for req in queue}|set(completed)
                        queue=[req.serializable() for req in following if stable_hash(req.serializable()) not in existing]+queue
                        if len(queue)>10000: raise ValueError('Bounded discovered request queue exceeded')
                        gaps=cursor.get('gaps',[])+([{'url':request.url,'reason':reason}] if reason else [])
                        blocked=list(cursor.get('blocked_hosts',[]))
                        if reason=='access_denied' and host=='ir.nasdaq.com' and host not in blocked: blocked.append(host)
                        next_cursor={**cursor,'queue':queue,'completed':completed,'blocked_hosts':blocked,'gaps':gaps}
                        with session.begin():
                            document=register_document(session,source=request.source,domain='index_history',url=request.url,
                                request_scope={'original_scope':original_scope(),'request':request.serializable(),'http_status':response.status_code,'content_type':response.content_type},
                                captured_at=response.captured_at,raw_root=raw_root,content=response.content)
                            stored=store_records(session,document,rows)
                            state=session.get(IndexAcquisitionState,state_id);state.cursor=next_cursor
                            state.status='source_queue_complete' if not queue else 'collecting';state.updated_at=utcnow()
                        progress['source_rows']+=stored['source_rows'];progress['inserted']+=stored['inserted'];progress['access_gaps']+=int(reason is not None)
                        cursor=next_cursor
                    status='source_queue_complete' if not cursor['queue'] else 'budget_deferred'
                    with session.begin():
                        state=session.get(IndexAcquisitionState,state_id);state.status=status;state.updated_at=utcnow()
                    return {'status':status,'coverage_complete':False,'canonical_eligible':False,'original_scope':original_scope(),
                            'progress':progress,'remaining_requests':len(cursor['queue']),'gaps':cursor.get('gaps',[])}
            finally:
                if connection.in_transaction(): connection.rollback()
                connection.execute(text('SELECT pg_advisory_unlock(hashtext(:key))'),{'key':lock});connection.commit()
    finally:
        if own: api.client.close()

def project_supplemental_history(engine,*,manifest_id,limit=100,budget_seconds=30):
    """Reuse immutable official catalog/change captures without fetching or rehashing."""
    if not 1<=limit<=1000 or not 0<budget_seconds<=3600: raise ValueError('Bounded projection required')
    deadline=time.monotonic()+budget_seconds;progress={'documents':0,'source_rows':0,'inserted':0,'gaps':0}
    with Session(engine) as session:
        while progress['documents']<limit and time.monotonic()<deadline:
            with session.begin():
                manifest=_manifest(session,manifest_id)
                processed=exists(select(IndexAcquisitionState.id).where(IndexAcquisitionState.manifest_id==manifest_id,
                    IndexAcquisitionState.job_key=='projection:'+SupplementalDocument.id))
                eligible=exists(select(SupplementalObservation.id).where(SupplementalObservation.document_id==SupplementalDocument.id,
                    SupplementalObservation.record_kind.in_(['index_announcement','index_member'])))
                document=session.scalar(select(SupplementalDocument).where(SupplementalDocument.source.in_(['lseg','spglobal']),eligible,~processed).order_by(SupplementalDocument.captured_at,SupplementalDocument.id).limit(1))
                if document is None: return {'status':'source_queue_complete','coverage_complete':False,'progress':progress}
                source_rows=session.scalars(select(SupplementalObservation).where(SupplementalObservation.document_id==document.id,SupplementalObservation.record_kind.in_(['index_announcement','index_member'])).order_by(SupplementalObservation.id)).all()
                rows=[];gap=False
                try:
                    for source in source_rows:
                        row=supplemental_record(source)
                        if _in_scope(row): rows.append(row)
                except ValueError:
                    gap=True;rows=[dict(record_kind='access_gap',index_key=f'OFFICIAL_SOURCE:{document.source}',row_key='projection_schema_gap',
                        fields={'reason':'unrecognized_supplemental_index_schema'},issues=['unrecognized_supplemental_index_schema','baseline_unavailable'])]
                stored=store_records(session,document,rows)
                state=_new_state(manifest,'projection:'+document.id,{'version':1,'document_id':document.id,'source_rows':len(source_rows),'projected_rows':len(rows),'gap':gap})
                state.status='source_queue_complete';session.add(state)
                progress['documents']+=1;progress['source_rows']+=stored['source_rows'];progress['inserted']+=stored['inserted'];progress['gaps']+=int(gap)
    return {'status':'budget_deferred','coverage_complete':False,'progress':progress}
