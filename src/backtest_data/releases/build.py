"""Seal exact SQL evidence membership and immutable catalog snapshots."""

from contextlib import contextmanager
from datetime import date,datetime,timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from sqlalchemy import column, func, inspect, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..catalog.models import (CatalogObservation,ExchangeSession,IdentifierHistory,Issuer,Security,SourceContract)
from ..db import new_id,utcnow
from ..fundamentals.models import (DerivedFact,Filing,FinancialSourceDocument,FundamentalFact,FundProductFact,SourceFinancialFact)
from ..fundamentals.publication_models import FinancialPublicationEvidence
from ..fundamentals.daily_publication_models import FinancialDailyPublicationEvidence
from ..ingestion.models import IngestionAttempt,RawObject,TargetUniverseManifest
from ..prices.models import CorporateActionObservation,PriceObservation,VWAPDailyStat
from .models import DataSegment,DatasetRelease,ReleaseSegment,SegmentMember,SelectionDecision,ValidationIssue
from .query_context import definition_registry,policy_bundle

MODELS={model.__tablename__:model for model in (ExchangeSession,IdentifierHistory,SourceContract,DerivedFact,Filing,
    FinancialSourceDocument,FinancialPublicationEvidence,FinancialDailyPublicationEvidence,FundamentalFact,FundProductFact,SourceFinancialFact,IngestionAttempt,RawObject,
    CorporateActionObservation,PriceObservation,VWAPDailyStat,SelectionDecision,ValidationIssue,TargetUniverseManifest)}
MAX_RELEASE_ROWS=1000000
BATCH_IDS=5000
DECISION_RULES={'raw_price_structure_v1','source_financial_structure_v1','sql_extraction_inventory_v1',
                'fundamental_mapping_v1','fundamental_mapping_v2','derived_metric_policy_v1','alpaca_raw_price_structure_v1',
                'kis_overseas_raw_price_structure_v1','kis_supplier_zero_transaction_structure_v1',
                'financial_semantic_completion_v1','provider_declared_price_structure_v1'}


def release_models(query_version='release_query_v5'):
    if query_version not in {'release_query_v5','release_query_v6','release_query_v7','release_query_v8_daily'}:raise ValueError('unsupported_query_version')
    return {key:value for key,value in MODELS.items()
        if (query_version!='release_query_v5' or key!='financial_publication_evidence')
        and (query_version=='release_query_v8_daily' or key!='financial_daily_publication_evidence')}


def price_scope_descriptor(target,scope,query_version='release_query_v7'):
    if not isinstance(scope,dict) or set(scope)!={'start','end'}:raise ValueError('price_scope_contract_required')
    start,end=scope['start'],scope['end']
    if type(start) is not date or type(end) is not date or start>end:raise ValueError('price_scope_dates_invalid')
    return {'kind':'price_sessions_v1','start':start.isoformat(),'end':end.isoformat(),
        'original_target_manifest_id':target.id,'original_target_content_hash':target.content_hash,
        'original_target_count':len(target.members),'allowed_profiles':['core_price_volume','security_identity'],
        'excluded_profiles':['fundamentals_sql','valuation','exact_vwap','index_membership'],
        'excluded_domains':['filing','financial_source_document','financial_publication_evidence',
            'source_financial_fact','fundamental_fact','derived_fact','fund_product_fact',
            'corporate_action_observation','vwap_daily_stat'],
        'raw_only':query_version!='release_query_v8_daily','full_period_complete':False,'historical_universe_complete':False}


def financial_scope_descriptor(target,scope,query_version='release_query_v6'):
    """Pin the requested document set and keep audit claims explicitly declared."""
    if not isinstance(scope,dict) or set(scope)!={'document_ids','audit_watermark','audit_counts'}:
        raise ValueError('financial_scope_contract_required')
    documents=scope['document_ids']
    if (not isinstance(documents,(list,tuple)) or not documents
            or any(not isinstance(identity,str) or not identity for identity in documents)
            or len(set(documents))!=len(documents)):
        raise ValueError('financial_scope_document_ids_invalid')
    watermark=scope['audit_watermark']
    if isinstance(watermark,str):
        try:watermark=datetime.fromisoformat(watermark)
        except ValueError as exc:raise ValueError('financial_scope_audit_watermark_invalid') from exc
    if not isinstance(watermark,datetime) or watermark.tzinfo is None or watermark.utcoffset() is None:
        raise ValueError('financial_scope_audit_watermark_invalid')
    counts=scope['audit_counts']
    keys={'total_documents','verified_documents','missing_documents'}
    if (not isinstance(counts,dict) or set(counts)!=keys
            or any(type(counts[key]) is not int or counts[key]<0 for key in keys)
            or counts['verified_documents']+counts['missing_documents']!=counts['total_documents']
            or len(documents)>counts['total_documents']):
        raise ValueError('financial_scope_audit_counts_invalid')
    return {'kind':'financial_documents_v1','document_ids':sorted(documents),
        'audit_watermark':watermark.astimezone(timezone.utc).isoformat(),
        'audit_counts':dict(counts),'audit_counts_source':'external_audit_declaration',
        'publication_policy_version':'financial_daily_metadata_pit_v1' if query_version=='release_query_v8_daily' else 'financial_public_pit_v1',
        'original_target_manifest_id':target.id,'original_target_content_hash':target.content_hash,
        'original_target_count':len(target.members),'excluded_domain':'prices',
        'allowed_profiles':['fundamentals_sql','security_identity'],
        'excluded_profiles':['core_price_volume','exact_vwap','valuation','index_membership'],
        'allowed_domains':['filing','financial_source_document',('financial_daily_publication_evidence' if query_version=='release_query_v8_daily' else 'financial_publication_evidence'),
            'source_financial_fact','fundamental_fact','derived_fact','identifier_history'],
        'excluded_domains':['price_observation','vwap_daily_stat','corporate_action_observation','fund_product_fact']}


def financial_scope_coverage(descriptor,rows):
    from ..fundamentals.public_availability import resolve_document_availability
    if any(rows.get(domain) for domain in descriptor['excluded_domains']):
        raise ValueError('financial_scope_excluded_dependency')
    documents=rows['financial_source_document']
    if not set(descriptor['document_ids']).issubset(documents):raise ValueError('financial_scope_document_missing')
    if len(documents)>descriptor['audit_counts']['total_documents']:raise ValueError('financial_scope_denominator_exceeded')
    watermark=datetime.fromisoformat(descriptor['audit_watermark'])
    verified=withdrawn=0
    daily=descriptor.get('publication_policy_version')=='financial_daily_metadata_pit_v1'
    evidence=list(rows.get('financial_daily_publication_evidence' if daily else 'financial_publication_evidence',{}).values())
    evidence_by_document={}
    for row in evidence:evidence_by_document.setdefault(row.document_id,[]).append(row)
    for document in documents.values():
        filing=rows['filing'].get(document.filing_id)
        if filing is None:raise ValueError('financial_scope_filing_missing')
        if daily:
            from ..fundamentals.daily_publication import resolve_daily_availability
            result=resolve_daily_availability(document,filing,evidence_by_document.get(document.id,[]),history_mode='reconstructed_public',as_of=watermark)
        else:
            result=resolve_document_availability(document,filing,evidence_by_document.get(document.id,[]),
                history_mode='reconstructed_public',as_of=watermark)
        verified+=result['status']=='qualified'
        withdrawn+=result.get('withdrawn',False)
    return {**descriptor,'included_document_ids':sorted(documents),'included_document_count':len(documents),
        'excluded_document_count':descriptor['audit_counts']['total_documents']-len(documents),
        'included_public_counts':{'total_documents':len(documents),'verified_documents':verified,
            'missing_documents':len(documents)-verified,'withdrawn_documents':withdrawn}}


def candidate_row_count(rows,catalog):
    return sum(len(bucket) for bucket in rows.values())+len(catalog['securities'])+len(catalog['issuers'])+len(catalog['observations'])


def json_value(value):
    if isinstance(value,datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value,date):return value.isoformat()
    if isinstance(value,Decimal):return str(value)
    if isinstance(value,dict):return {str(key):json_value(item) for key,item in value.items()}
    if isinstance(value,(tuple,list,set)):return [json_value(item) for item in value]
    return value


def canonical(value):
    return json.dumps(json_value(value),sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()


def id_batches(values):
    ordered=sorted(values)
    for offset in range(0,len(ordered),BATCH_IDS):yield ordered[offset:offset+BATCH_IDS]


def row_payload(row,*,exclude=()):
    return json_value({column.key:getattr(row,column.key) for column in inspect(type(row)).columns if column.key not in exclude})


def segment_digest(domain,rows):
    hashed=hashlib.sha256(('segment_rows_v1\n'+domain+'\n').encode())
    for row in sorted(rows,key=lambda item:item.id):
        hashed.update(canonical({'id':row.id,'row_hash':digest(row_payload(row))}).encode()+b'\n')
    return hashed.hexdigest()


@contextmanager
def database_session(database,*,write=False):
    if isinstance(database,Session):
        if write:
            with database.begin_nested():yield database
        else:yield database
        return
    with database.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
        with Session(connection) as session,session.begin():yield session


def input_ref(value):
    if not isinstance(value,dict):raise ValueError('undefined_dependency_reference')
    domain=value.get('domain',value.get('record_domain'))
    identity=value.get('id',value.get('record_id'))
    if domain not in MODELS or not identity:raise ValueError('undefined_dependency_reference')
    return domain,str(identity)


def directory_evidence(target):
    evidence=(target.scope or {}).get('directory_evidence',[])
    if not isinstance(evidence,list):raise ValueError('undefined_directory_evidence')
    for value in evidence:
        if not isinstance(value,dict) or not isinstance(value.get('raw_id'),str) or not value['raw_id']:
            raise ValueError('undefined_directory_evidence')
        expected=value.get('content_hash',value.get('hash'))
        if not isinstance(expected,str) or len(expected)!=64 or any(char not in '0123456789abcdef' for char in expected):
            raise ValueError('undefined_directory_evidence_hash')
        if 'content_hash' in value and 'hash' in value and value['content_hash']!=value['hash']:
            raise ValueError('directory_evidence_hash_mismatch')
    return evidence


def master_evidence(value):
    if not isinstance(value,dict):raise ValueError('undefined_master_evidence')
    if 'master_raw_id' not in value and 'master_hash' not in value:return None
    identity,expected=value.get('master_raw_id'),value.get('master_hash')
    if not isinstance(identity,str) or not identity:raise ValueError('undefined_master_evidence_reference')
    if not isinstance(expected,str) or len(expected)!=64 or any(char not in '0123456789abcdef' for char in expected):
        raise ValueError('undefined_master_evidence_hash')
    return identity,expected


def dependencies(row):
    refs=[]
    if isinstance(row,SourceFinancialFact):refs.append(('financial_source_document',row.document_id))
    if isinstance(row,FinancialSourceDocument):refs.append(('filing',row.filing_id))
    if isinstance(row,(FinancialPublicationEvidence,FinancialDailyPublicationEvidence)):
        refs.extend([('financial_source_document',row.document_id),('filing',row.filing_id)])
        if isinstance(row,FinancialDailyPublicationEvidence) and row.source_fact_id:refs.append(('source_financial_fact',row.source_fact_id))
    if isinstance(row,FundamentalFact):refs.append(('source_financial_fact',row.source_fact_id))
    if isinstance(row,(PriceObservation,VWAPDailyStat)):
        refs.extend([('raw_object',row.raw_id),('source_contract',row.source_contract_id)])
        mapping = (row.evidence or {}).get('current_mapping_evidence') or {}
        master=master_evidence(mapping)
        if master:refs.append(('raw_object',master[0]))
    if isinstance(row,FundProductFact):refs.append(('source_contract',row.source_contract_id))
    if isinstance(row,IngestionAttempt):refs.append(('source_contract',row.source_contract_id))
    if isinstance(row,SelectionDecision):
        if row.record_domain not in MODELS:raise ValueError('undefined_decision_domain')
        refs.append((row.record_domain,row.record_id))
    if isinstance(row,ValidationIssue):
        if row.record_id:refs.append(input_ref({'domain':row.record_domain,'id':row.record_id}))
        if row.evidence.get('raw_id'):refs.append(('raw_object',str(row.evidence['raw_id'])))
    if isinstance(row,TargetUniverseManifest):
        refs.extend(('raw_object',value['raw_id']) for value in directory_evidence(row))
    if isinstance(row,DerivedFact):refs.extend(input_ref(value) for value in row.input_refs)
    return refs


def financial_inventory(document,facts):
    counts=document.inventory.get('counts',{})
    if not counts:
        return ['financial_inventory_missing']
    emitted=sum(len(row.evidence.get('source_node_ids',[])) for row in facts)
    reasons=[]
    if counts.get('extracted_nodes')!=emitted or counts.get('numeric_nodes')!=emitted:
        reasons.append('financial_inventory_mismatch')
    if len(facts)+counts.get('deduplicated_nodes',0)!=emitted:
        reasons.append('financial_inventory_mismatch')
    contexts=document.inventory.get('contexts',{})
    if any(row.context_id not in contexts for row in facts):reasons.append('financial_context_inventory_mismatch')
    if not document.extraction_complete:reasons.append('financial_extraction_incomplete')
    return sorted(set(reasons))


def collect_candidate(session,target,*,query_version='release_query_v5',financial_scope=None,price_scope=None):
    models=release_models(query_version)
    if financial_scope is not None and query_version not in {'release_query_v6','release_query_v7','release_query_v8_daily'}:
        raise ValueError('financial_scope_requires_v6_or_v7')
    scope=financial_scope_descriptor(target,financial_scope,query_version) if financial_scope is not None else None
    if price_scope is not None and (financial_scope is not None or query_version not in {'release_query_v7','release_query_v8_daily'}):
        raise ValueError('price_scope_requires_exclusive_v7')
    price_range=price_scope_descriptor(target,price_scope,query_version) if price_scope is not None else None
    watermark=datetime.fromisoformat(scope['audit_watermark']) if scope else None
    excluded_domains=set((scope or price_range or {}).get('excluded_domains',[]))
    security_ids={item.get('security_id') for item in target.members}
    if None in security_ids or not security_ids:raise ValueError('full_target_security_identity_required')
    securities=session.scalars(select(Security).where(Security.id.in_(security_ids)).order_by(Security.id)).all()
    if len(securities)!=len(security_ids):raise ValueError('full_target_security_missing')
    issuer_ids={row.issuer_id for row in securities if row.issuer_id}
    issuer_ids.update(item['issuer_id'] for item in target.members if item.get('issuer_id'))
    issuers=session.scalars(select(Issuer).where(Issuer.id.in_(issuer_ids)).order_by(Issuer.id)).all()
    if len(issuers)!=len(issuer_ids):raise ValueError('full_target_issuer_missing')
    rows={domain:{} for domain in models}
    rows['target_universe_manifest'][target.id]=target
    count=1+(len(securities)+len(issuers) if query_version in {'release_query_v6','release_query_v7','release_query_v8_daily'} else 0)
    if count>MAX_RELEASE_ROWS:raise ValueError('release_row_budget_exceeded')

    def audit_visible(row):
        if watermark is None:return True
        # The audit fixes the original source inventory. New verification and
        # materialization events are frozen in this sealing snapshot, retaining
        # their real clocks for observed-system queries.
        if isinstance(row,SourceFinancialFact):clocks=[row.captured_at,row.extracted_at,row.first_seen_at]
        elif isinstance(row,(FinancialSourceDocument,Filing)):clocks=[row.captured_at]
        else:return True
        return all(value is None or value<=watermark for value in clocks)

    def increment_catalog():
        nonlocal count
        if query_version not in {'release_query_v6','release_query_v7','release_query_v8_daily'}:return
        count+=1
        if count>MAX_RELEASE_ROWS:raise ValueError('release_row_budget_exceeded')

    def add(row):
        nonlocal count
        if price_range:
            if isinstance(row,PriceObservation) and ((row.basis not in {'as_traded','provider_adjusted'} if query_version=='release_query_v8_daily' else row.basis!='as_traded') or
                    not price_scope['start']<=row.session<=price_scope['end']):
                raise ValueError('price_scope_dependency_outside_declared_sessions')
            if isinstance(row,ValidationIssue) and row.record_domain=='price_observation':
                if row.record_id is not None and row.record_id not in rows['price_observation']:return False
                value=(row.evidence or {}).get('session')
                if isinstance(value,str):
                    try:day=date.fromisoformat(value)
                    except ValueError:day=None
                    if day is not None and not price_scope['start']<=day<=price_scope['end']:return False
        if row.__tablename__ in excluded_domains:raise ValueError('financial_scope_excluded_dependency:'+row.__tablename__)
        if isinstance(row,(SelectionDecision,ValidationIssue)) and row.record_domain in excluded_domains:return False
        bucket=rows[row.__tablename__]
        if row.id in bucket:return False
        if not audit_visible(row):return False
        count+=1
        if count>MAX_RELEASE_ROWS:raise ValueError('release_row_budget_exceeded')
        bucket[row.id]=row
        return True

    def gather(model,predicate):
        for row in session.scalars(select(model).where(predicate).order_by(model.id).execution_options(populate_existing=True)).yield_per(500):add(row)

    if scope:
        for batch in id_batches(scope['document_ids']):gather(FinancialSourceDocument,FinancialSourceDocument.id.in_(batch))
        if set(rows['financial_source_document'])!=set(scope['document_ids']):raise ValueError('financial_scope_document_missing')
        for document in rows['financial_source_document'].values():
            filing=session.get(Filing,document.filing_id)
            if filing is None or filing.issuer_id not in issuer_ids:raise ValueError('financial_scope_document_outside_original_target')
        gather(IdentifierHistory,IdentifierHistory.security_id.in_(security_ids))
    elif price_range:
        gather(PriceObservation,PriceObservation.security_id.in_(security_ids) &
            (PriceObservation.session>=price_scope['start']) & (PriceObservation.session<=price_scope['end']) &
            (PriceObservation.basis.in_(['as_traded','provider_adjusted']) if query_version=='release_query_v8_daily' else PriceObservation.basis=='as_traded'))
        gather(IdentifierHistory,IdentifierHistory.security_id.in_(security_ids))
    else:
        for model in (Filing,SourceFinancialFact,FundamentalFact):gather(model,model.issuer_id.in_(issuer_ids))
        for model in (PriceObservation,VWAPDailyStat,CorporateActionObservation,FundProductFact,IdentifierHistory):
            gather(model,model.security_id.in_(security_ids))
    # A correction may reassign an alias to a security outside this target.
    # Seal the entire assertion history so the old target mapping cannot revive.
    assertion_keys={row.evidence['assertion_key'] for row in rows['identifier_history'].values()
                    if row.evidence.get('assertion_key')}
    for batch in id_batches(assertion_keys):
        gather(IdentifierHistory,IdentifierHistory.evidence['assertion_key'].astext.in_(batch))
    if not scope and not price_range:
        for batch in id_batches(rows['filing']):gather(FinancialSourceDocument,FinancialSourceDocument.filing_id.in_(batch))
        derived_predicate=DerivedFact.entity_id.in_(issuer_ids|security_ids)
        if query_version in {'release_query_v6','release_query_v7','release_query_v8_daily'}:
            versions=[query_version] if query_version=='release_query_v8_daily' else ['release_query_v6','release_query_v7'] if query_version=='release_query_v7' else ['release_query_v6']
            derived_predicate=derived_predicate & DerivedFact.basis['query_version'].astext.in_(versions) & (
                DerivedFact.basis['public_availability_policy'].astext==('financial_daily_metadata_pit_v1' if query_version=='release_query_v8_daily' else 'financial_public_pit_v1'))
        else:
            derived_predicate=derived_predicate & (func.coalesce(DerivedFact.basis['query_version'].astext,'')!='release_query_v6') & (
                func.coalesce(DerivedFact.basis['public_availability_policy'].astext,'').not_in(['financial_public_pit_v1','financial_daily_metadata_pit_v1'])) & (
                func.coalesce(DerivedFact.basis['query_version'].astext,'')!='release_query_v8_daily')
        gather(DerivedFact,derived_predicate)
    gather(ExchangeSession,ExchangeSession.market.in_(target.scope.get('markets',[])))
    known_security_ids={item.id for item in securities}
    known_issuer_ids={item.id for item in issuers}
    observations={}
    queried_observation_keys=set()

    def gather_catalog():
        # Catalog parents can also be introduced by alias/dependency closure.
        # Load their master captures within the same fixed point, so a master
        # brings its raw archive, attempts, decisions and issues into the seal.
        changed=False
        for bucket in rows.values():
            for row in bucket.values():
                identity=getattr(row,'security_id',None)
                if identity and identity not in known_security_ids:
                    parent=session.get(Security,identity)
                    if parent is None:raise ValueError('missing_catalog_dependency')
                    securities.append(parent)
                    known_security_ids.add(identity)
                    increment_catalog()
                identity=getattr(row,'issuer_id',None)
                if identity and identity not in known_issuer_ids:
                    parent=session.get(Issuer,identity)
                    if parent is None:raise ValueError('missing_catalog_dependency')
                    issuers.append(parent)
                    known_issuer_ids.add(identity)
                    increment_catalog()
        for security in securities:
            if security.issuer_id and security.issuer_id not in known_issuer_ids:
                parent=session.get(Issuer,security.issuer_id)
                if parent is None:raise ValueError('missing_catalog_dependency')
                issuers.append(parent)
                known_issuer_ids.add(parent.id)
                increment_catalog()
        observation_keys={row.source_identifier for row in securities}|{row.source_identifier for row in issuers}|set(rows['financial_source_document'])
        observation_keys.update(row.source_identifier.removeprefix('DART:') for row in issuers if row.source_identifier.startswith('DART:'))
        issuer_lookup={row.id:row for row in issuers}
        security_lookup={row.id:row for row in securities}
        for member in target.members:
            issuer=issuer_lookup.get(security_lookup[member['security_id']].issuer_id)
            if issuer and issuer.source_identifier.startswith('CIK') and member.get('symbol'):
                observation_keys.add(issuer.source_identifier.removeprefix('CIK')+':'+member['symbol'])
        for batch in id_batches(observation_keys-queried_observation_keys):
            for row in session.scalars(select(CatalogObservation).where(CatalogObservation.identifier.in_(batch)).order_by(CatalogObservation.id)):
                if not audit_visible(row):continue
                if row.id not in observations:increment_catalog()
                observations[row.id]=row_payload(row)
                master=master_evidence(row.fields)
                if master:
                    identity,expected=master
                    raw=rows['raw_object'].get(identity)
                    if raw is None:raw=session.get(RawObject,identity)
                    if raw is None:raise ValueError(f'missing_dependency:raw_object:{identity}')
                    if raw.content_hash!=expected:raise ValueError('master_evidence_hash_mismatch')
                    changed|=add(raw)
        queried_observation_keys.update(observation_keys)
        return changed

    gather_catalog()
    completed_documents=set()
    queried_derived_refs=set()

    def gather_complete_documents():
        changed=False
        for document_id in sorted(set(rows['financial_source_document'])-completed_documents):
            document=rows['financial_source_document'][document_id]
            if scope:
                facts=session.scalars(select(SourceFinancialFact).where(SourceFinancialFact.document_id==document_id).order_by(SourceFinancialFact.id)).all()
                if any(not audit_visible(fact) for fact in facts):raise ValueError('financial_scope_document_after_audit_watermark')
                for fact in facts:changed|=add(fact)
                for batch in id_batches({fact.id for fact in facts}):
                    for canonical_row in session.scalars(select(FundamentalFact).where(FundamentalFact.source_fact_id.in_(batch))):changed|=add(canonical_row)
                hard=[reason for reason in financial_inventory(document,facts) if reason!='financial_extraction_incomplete']
                if hard:raise ValueError('financial_scope_document_inventory_incomplete:'+hard[0])
            if query_version in {'release_query_v6','release_query_v7','release_query_v8_daily'}:
                proof_model=FinancialDailyPublicationEvidence if query_version=='release_query_v8_daily' else FinancialPublicationEvidence
                for evidence in session.scalars(select(proof_model).where(
                        proof_model.document_id==document_id).order_by(proof_model.id)):
                    changed|=add(evidence)
            completed_documents.add(document_id)
        return changed

    def gather_related_derived():
        if not scope:return False
        changed=False
        values=func.jsonb_array_elements(DerivedFact.input_refs).table_valued(column('value',JSONB)).alias('input_reference')
        ref_domain=func.coalesce(values.c.value['domain'].astext,values.c.value['record_domain'].astext)
        ref_id=func.coalesce(values.c.value['id'].astext,values.c.value['record_id'].astext)
        for domain in ('source_financial_fact','fundamental_fact','derived_fact',
                'financial_daily_publication_evidence' if query_version=='release_query_v8_daily' else 'financial_publication_evidence'):
            fresh={identity for identity in rows[domain] if (domain,identity) not in queried_derived_refs}
            for batch in id_batches(fresh):
                matching=select(1).select_from(values).where(ref_domain==domain,ref_id.in_(batch)).exists()
                statement=select(DerivedFact).where(DerivedFact.entity_id.in_(issuer_ids|security_ids),matching,
                    DerivedFact.basis['query_version'].astext.in_(
                        [query_version] if query_version=='release_query_v8_daily' else ['release_query_v6','release_query_v7'] if query_version=='release_query_v7' else ['release_query_v6']),
                    DerivedFact.basis['public_availability_policy'].astext==('financial_daily_metadata_pit_v1' if query_version=='release_query_v8_daily' else 'financial_public_pit_v1')).order_by(DerivedFact.id)
                for metric in session.scalars(statement):changed|=add(metric)
            queried_derived_refs.update((domain,identity) for identity in fresh)
        return changed
    # Expand all referenced parents, decisions and decision histories to a fixed point.
    changed=True
    while changed:
        changed=gather_complete_documents()|gather_related_derived()
        for bucket in list(rows.values()):
            for row in list(bucket.values()):
                for domain,identity in dependencies(row):
                    if domain in excluded_domains:raise ValueError('financial_scope_excluded_dependency:'+domain)
                    if domain not in rows:raise ValueError('v6_publication_dependency_in_v5_release')
                    if identity not in rows[domain]:
                        parent=session.get(MODELS[domain],identity)
                        if parent is None:raise ValueError(f'missing_dependency:{domain}:{identity}')
                        if not audit_visible(parent):raise ValueError('financial_scope_dependency_after_audit_watermark')
                        changed|=add(parent)
        for domain,bucket in list(rows.items()):
            if domain in {'selection_decision','validation_issue'} or not bucket:continue
            for batch in id_batches(bucket):
                for decision in session.scalars(select(SelectionDecision).where(SelectionDecision.record_domain==domain,
                        SelectionDecision.record_id.in_(batch)).order_by(SelectionDecision.id)):
                    changed|=add(decision)
                for issue in session.scalars(select(ValidationIssue).where(ValidationIssue.record_domain==domain,
                        ValidationIssue.record_id.in_(batch)).order_by(ValidationIssue.id)):
                    changed|=add(issue)
        # Rejected input rows may have no accepted domain-record ID. Their
        # raw archive or full-target security evidence still belongs to this seal.
        for batch in id_batches(rows['raw_object']):
            for issue in session.scalars(select(ValidationIssue).where(ValidationIssue.evidence['raw_id'].astext.in_(batch))):
                changed|=add(issue)
        for batch in id_batches(security_ids):
            for issue in session.scalars(select(ValidationIssue).where(ValidationIssue.evidence['security_id'].astext.in_(batch))):
                changed|=add(issue)
        if rows['raw_object']:
            hashes={row.content_hash for row in rows['raw_object'].values()}
            for batch in id_batches(hashes):
                for attempt in session.scalars(select(IngestionAttempt).where(IngestionAttempt.content_hash.in_(batch))):changed|=add(attempt)
        changed|=gather_catalog()
    for value in directory_evidence(target):
        expected=value.get('content_hash',value.get('hash'))
        if rows['raw_object'][value['raw_id']].content_hash!=expected:
            raise ValueError('directory_evidence_hash_mismatch')
    for domain in ('price_observation','vwap_daily_stat'):
        for row in rows[domain].values():
            master=master_evidence((row.evidence or {}).get('current_mapping_evidence') or {})
            if master and rows['raw_object'][master[0]].content_hash!=master[1]:
                raise ValueError('master_evidence_hash_mismatch')
    catalog={'securities':{row.id:row_payload(row) for row in securities},'issuers':{row.id:row_payload(row) for row in issuers},
             'observations':[observations[identity] for identity in sorted(observations)],'snapshot_scope':'sealed_current_catalog'}
    if query_version in {'release_query_v6','release_query_v7','release_query_v8_daily'} and candidate_row_count(rows,catalog)>MAX_RELEASE_ROWS:
        raise ValueError('release_row_budget_exceeded')
    return rows,catalog,security_ids,issuer_ids


def build_release(database,manifest_id,*,raw_root=None,query_version='release_query_v5',financial_scope=None,price_scope=None):
    from .validation import validate_rows
    release_models(query_version)
    if financial_scope is not None and query_version not in {'release_query_v6','release_query_v7','release_query_v8_daily'}:
        raise ValueError('financial_scope_requires_v6_or_v7')
    policies=policy_bundle(query_version)
    if raw_root is None:
        from ..config import Settings
        raw_root=Settings.from_env().raw_root
    with database_session(database,write=True) as session:
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext('dataset_release_build_v1'))"))
        target=session.get(TargetUniverseManifest,manifest_id)
        if target is None:raise ValueError('target_manifest_not_found')
        rows,catalog,security_ids,issuer_ids=collect_candidate(session,target,query_version=query_version,financial_scope=financial_scope,price_scope=price_scope)
        documents=list(rows['financial_source_document'].values())
        if any(row.disclosure_profile!='periodic_report' for row in documents):raise ValueError('unsupported_or_mixed_disclosure_policy')
        for row in rows['selection_decision'].values():
            if row.rule_version not in DECISION_RULES:raise ValueError('undefined_selection_policy')
        definitions={}
        for row in rows['derived_fact'].values():
            if row.definition_version not in policies['metric_definitions'] or row.basis.get('disclosure_profile')!='periodic_report':
                raise ValueError('undefined_metric_or_disclosure_policy')
            identity=(row.definition_version,row.definition_hash,row.basis.get('numeric_policy'))
            definitions.setdefault(row.metric_id,set()).add(identity)
        if any(len(values)>1 for values in definitions.values()):raise ValueError('mixed_metric_definition_policy')
        integrity=validate_rows(rows,raw_root,policies=policies) if query_version in {'release_query_v6','release_query_v7','release_query_v8_daily'} else validate_rows(rows,raw_root)
        hard=[item for item in integrity if item['severity']=='error']
        if hard:raise ValueError('release_integrity_failure:'+hard[0]['reason'])
        gaps=list(target.gaps)+integrity
        if not target.scope.get('historical_universe_complete'):gaps.append({'reason':'historical_universe_incomplete','severity':'gap'})
        priced_security_ids={row.security_id for row in rows['price_observation'].values()}
        financial_issuer_ids={row.issuer_id for row in rows['source_financial_fact'].values()}
        for identity in sorted(security_ids):
            sec=catalog['securities'][identity]
            if sec['security_type'] in {'unclassified','etp_unknown','equity_or_note_unknown'}:
                gaps.append({'entity_id':identity,'reason':'security_classification_unqualified','severity':'gap'})
            if not sec['issuer_id']:gaps.append({'entity_id':identity,'reason':'issuer_mapping_unavailable','severity':'gap'})
            if financial_scope is None and identity not in priced_security_ids:
                gaps.append({'entity_id':identity,'reason':'prices_unavailable','severity':'gap'})
            if not sec['issuer_id'] or sec['issuer_id'] not in financial_issuer_ids:
                gaps.append({'entity_id':identity,'reason':'financial_source_accounts_unavailable','severity':'gap'})
        if any(row.evidence.get('availability_precision')=='capture_only' for row in rows['price_observation'].values()):
            gaps.append({'reason':'capture_only_price_history','severity':'gap'})
        if not rows['exchange_session']:gaps.append({'reason':'calendar_evidence_unavailable','severity':'gap'})
        if query_version=='release_query_v5' and any(not row.evidence.get('original_publication_verified') for row in rows['filing'].values()):
            gaps.append({'reason':'historical_disclosure_time_unqualified','severity':'gap'})
        if financial_scope is not None:
            gaps.append({'reason':'financial_document_scope_partial','severity':'gap'})
            gaps.append({'reason':'prices_scope_excluded','severity':'gap'})
        gaps.append({'reason':'production_activation_not_qualified','severity':'gap'})
        descriptors=[]
        segments=[]
        for domain,bucket in sorted(rows.items()):
            if not bucket:continue
            digest_value=segment_digest(domain,list(bucket.values()))
            session.execute(insert(DataSegment).values(id=new_id(),domain=domain,partition_key='exact-membership-v1',content_hash=digest_value)
                .on_conflict_do_nothing(index_elements=[DataSegment.content_hash]))
            segment=session.scalar(select(DataSegment).where(DataSegment.content_hash==digest_value))
            existing=session.scalars(select(SegmentMember).where(SegmentMember.segment_id==segment.id)).all()
            if not existing:
                values=[{'segment_id':segment.id,'record_domain':domain,'record_id':identity} for identity in sorted(bucket)]
                for offset in range(0,len(values),500):session.execute(insert(SegmentMember).values(values[offset:offset+500]))
            elif {(row.record_domain,row.record_id) for row in existing}!={(domain,identity) for identity in bucket}:
                raise ValueError('segment_membership_hash_conflict')
            segments.append(segment)
            descriptors.append({'id':segment.id,'domain':domain,'content_hash':digest_value,'count':len(bucket)})
        manifest={'format_version':'sealed_release_v1','target_manifest':row_payload(target),'catalog':catalog,
            'target_security_ids':sorted(security_ids),'target_issuer_ids':sorted(issuer_ids),'target_count':len(target.members),
            'tier':'research_partial','segments':descriptors,'policies':policies,
            'parser_versions':sorted({row.parser_version for row in documents}),
            'mapping_versions':sorted({row.mapping_version for row in rows['fundamental_fact'].values()}),
            'metric_definitions':{key:sorted(values) for key,values in sorted(definitions.items())},
            'metric_definition_registry':definition_registry(),
            'calendar_versions':sorted({row.calendar_version for row in rows['exchange_session'].values()}),
            'coverage':{'profiles':{key:False for key in ('fundamentals_sql','core_price_volume','valuation','exact_vwap','index_membership','security_identity')},
                        'security_aliases':{'security_ids_with_assertions':len({row.security_id for row in rows['identifier_history'].values()} & security_ids),
                                            'target_security_count':len(security_ids),'historical_identity_qualified':False},
                        'gaps':gaps,'domain_counts':{key:len(value) for key,value in rows.items()}},
            'schema_revision':session.execute(text('SELECT version_num FROM alembic_version')).scalar_one(),
            'code_hash':digest({**{path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(Path(__file__).parent.glob('*.py'))},
                                'catalog/aliases.py':hashlib.sha256((Path(__file__).parent.parent/'catalog'/'aliases.py').read_bytes()).hexdigest()})}
        if financial_scope is not None:
            manifest['research_scope']=financial_scope_coverage(financial_scope_descriptor(target,financial_scope,query_version),rows)
            manifest['research_scope']['evidence_snapshot_at']=utcnow().isoformat()
            manifest['research_scope']['sealed_membership_contract']='exact_membership_v1'
            manifest['research_scope']['publication_evidence_ids']=sorted(rows['financial_daily_publication_evidence' if query_version=='release_query_v8_daily' else 'financial_publication_evidence'])
            manifest['coverage']['financial_public_pit']=manifest['research_scope']['included_public_counts']
            if manifest['research_scope']['included_public_counts']['missing_documents']:
                gaps.append({'reason':'historical_disclosure_time_unqualified','severity':'gap'})
        elif query_version=='release_query_v8_daily':
            from ..fundamentals.daily_publication import resolve_daily_availability
            by_document={}
            for evidence in rows['financial_daily_publication_evidence'].values():by_document.setdefault(evidence.document_id,[]).append(evidence)
            missing=sum(resolve_daily_availability(document,rows['filing'][document.filing_id],by_document.get(document.id,[]),
                history_mode='reconstructed_public',as_of=utcnow())['status']!='qualified' for document in documents)
            manifest['coverage']['financial_public_pit']={'total_documents':len(documents),'verified_documents':len(documents)-missing,
                'missing_documents':missing,'evidence_grade':'metadata_grade','original_archive_verified':False}
        elif query_version in {'release_query_v6','release_query_v7'}:
            from ..fundamentals.public_availability import resolve_document_availability
            evidence_by_document={}
            for evidence in rows['financial_publication_evidence'].values():evidence_by_document.setdefault(evidence.document_id,[]).append(evidence)
            missing=sum(resolve_document_availability(document,rows['filing'][document.filing_id],evidence_by_document.get(document.id,[]),
                history_mode='reconstructed_public',as_of=utcnow())['status']!='qualified' for document in documents)
            manifest['coverage']['financial_public_pit']={'total_documents':len(documents),
                'verified_documents':len(documents)-missing,'missing_documents':missing}
            if missing:gaps.append({'reason':'historical_disclosure_time_unqualified','severity':'gap'})
        if price_scope is not None:
            manifest['research_scope']={**price_scope_descriptor(target,price_scope,query_version),
                'evidence_snapshot_at':utcnow().isoformat(),'sealed_membership_contract':'exact_membership_v1',
                'included_observations':len(rows['price_observation']),
                'securities_with_prices':len(priced_security_ids & security_ids)}
            gaps.append({'reason':'declared_price_session_scope_partial','severity':'gap'})
        content_hash=digest(manifest)
        existing=session.scalar(select(DatasetRelease).where(DatasetRelease.content_hash==content_hash))
        if existing is None:
            existing=DatasetRelease(manifest=json_value(manifest),content_hash=content_hash,status='building',sealed_at=utcnow())
            session.add(existing);session.flush()
            session.execute(insert(ReleaseSegment).values([{'release_id':existing.id,'segment_id':segment.id} for segment in segments]))
            existing.status='sealed';session.flush()
        result={'status':existing.status,'release_id':existing.id,'tier':'research_partial','target_count':len(target.members),
                'content_hash':existing.content_hash,'counts':manifest['coverage']['domain_counts'],'gaps':gaps}
        if financial_scope is not None or price_scope is not None:result['research_scope']=manifest['research_scope']
        return result
