"""Hash-bound financial audits and resumable, memory-only original verification."""
from collections import Counter
from dataclasses import replace
from datetime import datetime,timezone
import json
import math
from pathlib import Path
import time

from sqlalchemy import func,select,text
from sqlalchemy.orm import Session

from ..db import utcnow
from ..ingestion.models import TargetUniverseManifest
from ..ingestion.http import ProviderError,ProviderHTTP
from ..releases.build import database_session,json_value
from .domain import stable_hash
from .models import Filing,FinancialSourceDocument,SourceFinancialFact
from .public_availability import POLICY_VERSION
from .public_version_batch import load_publication_batch,run_publication_batch
from .public_version_sources import ArchiveLimits


def _write(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    pending=path.with_name(path.name+'.pending')
    pending.write_text(json.dumps(json_value(value),ensure_ascii=False,indent=2),encoding='utf-8')
    pending.replace(path)


def validate_audit(value):
    if not isinstance(value,dict) or value.get('format_version')!='financial_publication_audit_v1' or value.get('policy_version')!=POLICY_VERSION:
        raise ValueError('unsupported_publication_audit')
    if value.get('content_hash')!=stable_hash({key:item for key,item in value.items() if key!='content_hash'}):
        raise ValueError('publication_audit_hash_mismatch')
    watermark=datetime.fromisoformat(value['watermark'])
    if watermark.tzinfo is None or watermark.utcoffset() is None:raise ValueError('aware_audit_watermark_required')
    ids=[document['id'] for document in value['documents']]
    if len(set(ids))!=len(ids) or any(not isinstance(identity,str) or not identity for identity in ids):
        raise ValueError('publication_audit_document_ids_invalid')
    if not value.get('original_target',{}).get('security_count'):raise ValueError('original_full_target_required')
    return value


def validate_progress(progress,audit):
    if progress.get('audit_hash')!=audit['content_hash']:raise ValueError('publication_progress_audit_target_mismatch')
    ids={document['id'] for document in audit['documents']}
    if progress.get('cursor') is not None and progress['cursor'] not in ids:raise ValueError('publication_progress_cursor_outside_audit')
    if not set(progress.get('results',{})).issubset(ids):raise ValueError('publication_progress_result_outside_audit')
    return progress


def audit_publications(database,*,manifest_id,output_path,watermark=None):
    """Freeze every stored SEC/DART document, not a sample of the full target."""
    watermark=watermark or utcnow()
    if watermark.tzinfo is None or watermark.utcoffset() is None:raise ValueError('aware_audit_watermark_required')
    path=Path(output_path)
    if path.exists():raise ValueError('publication_audit_exists_preserve_original')
    with database_session(database) as session:
        target=session.get(TargetUniverseManifest,manifest_id)
        if target is None:raise ValueError('original_target_manifest_not_found')
        statement=select(FinancialSourceDocument.id,FinancialSourceDocument.content_hash,FinancialSourceDocument.captured_at,
            FinancialSourceDocument.parser_version,FinancialSourceDocument.extraction_complete,Filing.source,Filing.issuer_id,
            Filing.accession).join(Filing,FinancialSourceDocument.filing_id==Filing.id).where(
                FinancialSourceDocument.captured_at<=watermark,Filing.source.in_(('sec','opendart'))).order_by(FinancialSourceDocument.id)
        documents=[json_value(dict(row)) for row in session.execute(statement).mappings()]
        fact_counts=dict(session.execute(select(SourceFinancialFact.document_id,func.count()).join(
            FinancialSourceDocument,SourceFinancialFact.document_id==FinancialSourceDocument.id).where(
                FinancialSourceDocument.captured_at<=watermark).group_by(SourceFinancialFact.document_id)).all())
        for document in documents:document['source_fact_count']=fact_counts.get(document['id'],0)
        audit={'format_version':'financial_publication_audit_v1','policy_version':POLICY_VERSION,
            'watermark':watermark.astimezone(timezone.utc).isoformat(),
            'original_target':{'manifest_id':target.id,'content_hash':target.content_hash,'security_count':len(target.members),
                'market_counts':dict(Counter(item['market'] for item in target.members))},
            'documents':documents,'counts':{'total_documents':len(documents),
                'source_facts':sum(item['source_fact_count'] for item in documents),
                'by_source':dict(Counter(item['source'] for item in documents)),
                'incomplete_documents':sum(not item['extraction_complete'] for item in documents)}}
    audit['content_hash']=stable_hash(audit)
    validate_audit(audit)
    _write(path,audit)
    return {'status':'audit_ready','audit_hash':audit['content_hash'],'audit_path':str(path),
        'watermark':audit['watermark'],'original_target':audit['original_target'],'counts':audit['counts']}


class _OriginalTransport:
    def __init__(self,http,credentials):
        self.http,self.credentials=http,credentials
        self.blocked_stages={}
        self.sec_archive_failures={}

    def __call__(self,request):
        if request.source=='sec':
            if '@' not in self.credentials.sec_user_agent:raise ProviderError('sec','requires_identifying_user_agent')
            key=stable_hash((request.url,request.params,request.max_bytes))
            if key in self.sec_archive_failures:raise ProviderError('sec',self.sec_archive_failures[key])
            try:
                return self.http.request('sec','GET',request.url,params=request.params,
                    headers={'User-Agent':self.credentials.sec_user_agent,'Accept-Encoding':'identity'},max_bytes=request.max_bytes)
            except ProviderError as exc:
                # Each daily archive can serve many documents. Cache only the
                # bounded-download/access failures for this run, without source
                # bodies; transient failures remain retryable on later batches.
                if not exc.retryable and exc.code in {'403','404','response_size_exceeded'}:
                    self.sec_archive_failures[key]=exc.code
                raise
        if request.source!='opendart' or request.url not in {
            'https://opendart.fss.or.kr/api/list.json','https://opendart.fss.or.kr/api/fnlttXbrl.xml'}:
            raise ValueError('unsupported_original_request')
        if not self.credentials.opendart_api_key:raise ProviderError('opendart','requires_credentials')
        if request.url in self.blocked_stages:raise ProviderError('opendart','source_stage_blocked',retryable=True)
        response=self.http.request('opendart','GET',request.url,
            params={**request.params,'crtfc_key':self.credentials.opendart_api_key.reveal()},max_bytes=request.max_bytes)
        if request.url.endswith('fnlttXbrl.xml') and not response.content.startswith(b'PK'):
            import re
            match=re.search(rb'<status>\s*(\d{3})\s*</status>',response.content[:8192])
            code=match.group(1).decode('ascii') if match else 'expected_zip_unavailable'
            if code=='800':self.blocked_stages[request.url]=code
            raise ProviderError('opendart',code,retryable=code=='800')
        return response


def verify_publications(database,*,audit_path,progress_path,budget_seconds=300,batch_size=5,
        retry_unavailable=False,fetch=None,archive_compressed_mb=64,archive_total_mb=512):
    """Commit bounded document outcomes and a contiguous cursor after each batch.

    A complete pass means every audited document has an outcome. Unqualified
    documents remain explicit gaps; this never declares the dataset complete.
    """
    if not math.isfinite(budget_seconds) or budget_seconds<=0:raise ValueError('positive_verification_budget_required')
    if type(batch_size) is not int or not 1<=batch_size<=50:raise ValueError('bounded_verification_batch_required')
    if (type(archive_compressed_mb) is not int or not 1<=archive_compressed_mb<=512
            or type(archive_total_mb) is not int or not 1<=archive_total_mb<=8192):
        raise ValueError('bounded_archive_resource_limits_required')
    limits=ArchiveLimits(max_compressed_bytes=archive_compressed_mb*1024*1024,
        max_decompressed_bytes=archive_total_mb*1024*1024)
    audit=validate_audit(json.loads(Path(audit_path).read_text(encoding='utf-8-sig')))
    path=Path(progress_path)
    progress=validate_progress(json.loads(path.read_text(encoding='utf-8-sig')),audit) if path.exists() else {
        'audit_hash':audit['content_hash'],'cursor':None,'results':{},'passes':[],'status':'pending'}
    if retry_unavailable:
        progress['passes'].append({'cursor':progress.get('cursor'),'status':progress.get('status'),
            'reason_counts':progress.get('reason_counts',{}),'finished_at':progress.get('checked_at')})
        progress.update(cursor=None,status='pending')
    if progress.get('status')=='audit_pass_complete' and not retry_unavailable:return _summary(progress,audit,path)
    watermark=datetime.fromisoformat(audit['watermark'])
    documents={item['id']:item for item in audit['documents']}
    progress['archive_resource_limits']={'compressed_mb':archive_compressed_mb,'total_expansion_mb':archive_total_mb,
        'member_bytes':limits.max_member_bytes,'source_bodies_saved':False}
    started=time.monotonic()
    http=None
    if fetch is None:
        from ..credentials import load_credentials
        http=ProviderHTTP(database)
        fetch=_OriginalTransport(http,load_credentials(sources=["opendart","sec"]))
    try:
        with database.connect().execution_options(isolation_level='READ COMMITTED') as connection,Session(connection) as session:
            if not session.scalar(text("SELECT pg_try_advisory_lock(hashtext('financial_publication_verify_v1'))")):
                raise ValueError('publication_verifier_already_running')
            session.commit()
            try:
                while time.monotonic()-started<budget_seconds:
                    with session.begin():
                        targets=load_publication_batch(session,watermark=watermark,after_document_id=progress['cursor'],
                            limit=batch_size,document_ids=tuple(documents))
                        if not targets:
                            missing=set(documents)-set(progress['results'])
                            progress['status']='audit_target_missing' if missing else 'audit_pass_complete'
                            progress['missing_document_ids']=sorted(missing)
                            break
                        checked=[]
                        for target in targets:
                            expected=documents[target.document.id]
                            if target.document.content_hash!=expected['content_hash']:
                                target=replace(target,preflight_reason='audit_document_hash_mismatch')
                            elif (isinstance(fetch,_OriginalTransport) and any(url.endswith('fnlttXbrl.xml') for url in fetch.blocked_stages)
                                    and target.filing.source=='opendart' and not str(target.document.parser_version).startswith('opendart_financial_json')):
                                target=replace(target,preflight_reason='dart_original_stage_800')
                            checked.append(target)
                        result=run_publication_batch(session,targets=checked,watermark=watermark,fetch=fetch,
                            original_target_count=len(documents),limits=limits)
                    # The SQL transaction is committed before persisting cursor.
                    # Crash between commit and write safely replays idempotently.
                    for item in result['results']:progress['results'][item['document_id']]=item
                    if result['next_cursor'] is not None:progress['cursor']=result['next_cursor']
                    progress['status']='sql_append_blocked' if result['sql_failed_count'] else 'running'
                    progress['checked_at']=utcnow().isoformat()
                    progress['reason_counts']=dict(Counter(item['reason'] for item in progress['results'].values() if item['reason']))
                    _write(path,progress)
                    if result['sql_failed_count']:break
                else:progress['status']='budget_deferred'
            finally:
                if session.in_transaction():session.rollback()
                session.execute(text("SELECT pg_advisory_unlock(hashtext('financial_publication_verify_v1'))"));session.commit()
    finally:
        if http:http.close()
    progress['checked_at']=utcnow().isoformat()
    progress['reason_counts']=dict(Counter(item['reason'] for item in progress['results'].values() if item['reason']))
    _write(path,progress)
    return _summary(progress,audit,path)


def _summary(progress,audit,path):
    counts=Counter(item['status'] for item in progress['results'].values())
    return {'status':progress['status'],'audit_hash':audit['content_hash'],'original_target':audit['original_target'],
        'total_documents':len(audit['documents']),'documents_with_outcomes':len(progress['results']),
        'qualified_documents':counts['qualified']+counts['already_qualified'],
        'unavailable_documents':counts['unavailable'],'pending_documents':len(audit['documents'])-len(progress['results']),
        'reason_counts':progress['reason_counts'],'progress_path':str(path),'coverage_complete':False}
