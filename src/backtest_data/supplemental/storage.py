"""Immutable typed source captures. No report/filing day is promoted to public time."""
from datetime import date, datetime
from decimal import Decimal
import hashlib
import gzip
from pathlib import Path
import shutil
from urllib.parse import urlsplit,urlunsplit
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from ..catalog.sources import ensure_contract
from ..db import new_id
from ..ingestion.jobs import stable_hash
from ..ingestion.models import RawObject
from ..ingestion.raw_store import RawStore
from .models import SupplementalDocument, SupplementalObservation

KINDS={'product','holding','distribution','index_announcement','index_member','listing_snapshot',
       'listing_event','nport_filing','nport_fund','nport_holding','source_gap','corporate_action',
       'trade_aggregate','fund_mapping'}
DATE_FIELDS=('report_date','filing_date','published_date','announced_date','effective_date','ex_date','record_date','pay_date','maturity_date','ipo_date','delisting_date')
NUMERIC_FIELDS=('quantity','value','weight','nav','net_assets','total_assets','total_liabilities','fee_rate','price','sum_price_volume','eligible_volume')
TEXT_FIELDS=('entity_name','member_identifier','member_name','product_type','currency')

def public_request(request):
    if isinstance(request,dict):
        return {str(k):public_request(v) for k,v in request.items() if not any(s in str(k).lower() for s in ('key','secret','auth','token','password','credential'))}
    if isinstance(request,(list,tuple)):return [public_request(v) for v in request]
    return request

def exact_json(value):
    if isinstance(value,Decimal):
        if not value.is_finite():raise ValueError('Nonfinite exact numeric input')
        return str(value)
    if isinstance(value,(datetime,date)):return value.isoformat()
    if isinstance(value,dict):return {str(k):exact_json(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [exact_json(v) for v in value]
    if isinstance(value,float):raise ValueError('Exact source rows must not contain float values')
    return value

def normalize_stage_row(row,captured_at):
    if not isinstance(captured_at,datetime) or captured_at.tzinfo is None:raise ValueError('Aware capture time required')
    if row.get('record_kind') not in KINDS or not row.get('entity_key') or not row.get('row_key'):raise ValueError('Valid staged row identity required')
    result={k:row[k] for k in ('record_kind','entity_key','row_key')}
    for name in DATE_FIELDS:
        value=row.get(name)
        if value is not None and type(value) is not date:raise ValueError('Exact source date required')
        result[name]=value
    for name in ('published_at','event_timestamp'):
        value=row.get(name)
        if value is not None and (not isinstance(value,datetime) or value.tzinfo is None):raise ValueError('Aware source clock required')
        result[name]=value
    for name in NUMERIC_FIELDS:
        value=row.get(name)
        if value is not None:
            if isinstance(value,bool) or not isinstance(value,(Decimal,int)):raise ValueError('exact Decimal numeric input required')
            value=Decimal(value)
            if not value.is_finite() or abs(value.as_tuple().exponent)>1000 or len(value.as_tuple().digits)>1000:raise ValueError('Exact numeric envelope exceeded')
        result[name]=value
    for name in TEXT_FIELDS:result[name]=None if row.get(name) is None else str(row[name])
    issues=list(row.get('issues',[]))
    if result['effective_date'] and result['effective_date']>captured_at.date():issues.append('future_effective_event')
    result.update(fields=exact_json(row.get('fields',{})),issues=issues,captured_at=captured_at,
        date_basis=row.get('date_basis','capture_only_publication_unverified'),canonical_eligible=False)
    return result

def archive_file(path,raw_root):
    path,root=Path(path),Path(raw_root)
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    hexdigest=digest.hexdigest(); key=f'sha256/{hexdigest[:2]}/{hexdigest}.gz'
    destination=RawStore(root)._path(key)
    if not destination.exists():
        destination.parent.mkdir(parents=True,exist_ok=True)
        pending=destination.with_suffix('.supplemental.tmp')
        with path.open('rb') as input_stream,pending.open('wb') as output_stream:
            with gzip.GzipFile(fileobj=output_stream,mode='wb',filename='',mtime=0) as compressed:
                shutil.copyfileobj(input_stream,compressed,1024*1024)
        pending.replace(destination)
    check=hashlib.sha256();byte_count=0
    try:
        with gzip.open(destination,'rb') as archived:
            for chunk in iter(lambda:archived.read(1024*1024),b''):
                byte_count+=len(chunk)
                if byte_count>path.stat().st_size:raise ValueError('Archive integrity failure')
                check.update(chunk)
    except (OSError,EOFError):raise ValueError('Archive integrity failure') from None
    if check.hexdigest()!=hexdigest or byte_count!=path.stat().st_size:raise ValueError('Archive integrity failure')
    return {'content_hash':hexdigest,'storage_key':key,'byte_count':path.stat().st_size}

def register_document(session,*,source,domain,url,request_scope,captured_at,raw_root,content=None,raw_metadata=None):
    if captured_at.tzinfo is None:raise ValueError('Aware capture time required')
    parts=urlsplit(url)
    if parts.scheme!='https' or parts.username or parts.password:raise ValueError('HTTPS public source URL required')
    public_url=urlunsplit((parts.scheme,parts.netloc,parts.path,'',''))
    if raw_metadata is None:
        stored=RawStore(raw_root).put(content)
        raw_metadata={'content_hash':stored.content_hash,'storage_key':stored.storage_key,'byte_count':stored.byte_count}
    session.execute(insert(RawObject).values(id=new_id(),**raw_metadata).on_conflict_do_nothing(index_elements=[RawObject.content_hash]))
    raw=session.scalar(select(RawObject).where(RawObject.content_hash==raw_metadata['content_hash']))
    spec={'source_url':public_url,'domain':domain,'qualification':'source_stage_only','clock_policy':'separate_capture_report_filing_publication_effective','canonical_adoption':False}
    contract=ensure_contract(session,source=source,domain=domain,market='GLOBAL',specification=spec)
    signature=stable_hash(['supplemental_document_v1',source,domain,public_url,public_request(request_scope),raw.content_hash,captured_at.isoformat()])
    session.execute(insert(SupplementalDocument).values(id=new_id(),source_contract_id=contract.id,raw_id=raw.id,
        source=source,domain=domain,source_url=public_url,content_hash=raw.content_hash,signature=signature,
        captured_at=captured_at,request_scope=exact_json(public_request(request_scope)),
        evidence={'coverage_complete':False,'publication_verification':'not_inferred','canonical_eligible':False})
        .on_conflict_do_nothing(index_elements=[SupplementalDocument.signature]))
    return session.scalar(select(SupplementalDocument).where(SupplementalDocument.signature==signature))

def store_rows(session,document,rows):
    batch=[]
    for row in rows:
        value=normalize_stage_row(row,document.captured_at)
        signature=stable_hash(['supplemental_row_v1',document.id,exact_json(value)])
        batch.append({'id':new_id(),'document_id':document.id,'signature':signature,**value})
    inserted=0
    for offset in range(0,len(batch),100):
        inserted+=len(session.execute(insert(SupplementalObservation).values(batch[offset:offset+100])
            .on_conflict_do_nothing(index_elements=[SupplementalObservation.signature]).returning(SupplementalObservation.id)).all())
    return {'source_rows':len(batch),'inserted':inserted,'document_id':document.id,'coverage_complete':False,'canonical_eligible':False}
