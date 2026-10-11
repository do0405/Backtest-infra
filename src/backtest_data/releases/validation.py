"""Domain-specific SQL closure, extraction and price archive integrity checks."""

import gzip
import hashlib
from pathlib import Path
import re
from collections import defaultdict
from datetime import timezone

from sqlalchemy import select

from ..ingestion.raw_store import RawStore
from .build import MODELS,database_session,dependencies,digest,financial_inventory,id_batches,row_payload,segment_digest
from .models import DataSegment,DatasetRelease,ReleaseSegment,SegmentMember
from .query_context import registered_definition,valid_metric_state


def raw_issue(raw,raw_root):
    try:
        path=RawStore(Path(raw_root))._path(raw.storage_key)
        if not path.is_file():return 'raw_archive_missing'
        if not 0<=raw.byte_count<=128*1024*1024:return 'raw_size_envelope_exceeded'
        with gzip.open(path,'rb') as stream:content=stream.read(raw.byte_count+1)
        if len(content)!=raw.byte_count or hashlib.sha256(content).hexdigest()!=raw.content_hash:
            return 'raw_checksum_mismatch'
    except (OSError,EOFError,ValueError):return 'raw_checksum_mismatch'
    return None


def validate_rows(rows,raw_root,*,policies=None):
    query_version=(policies or {}).get('query_version')
    daily=query_version=='release_query_v8_daily'
    # Plain sealed buckets need the pinned policies for availability projection;
    # no live metadata or evidence lookup is permitted during validation.
    if daily and not hasattr(rows,'manifest'):
        from .query import QueryRows
        rows=QueryRows(rows,{'policies':policies})
    issues=[]
    def add(reason,domain,identity=None,severity='error'):
        issues.append({'reason':reason,'domain':domain,'record_id':identity,'severity':severity})
    for domain,bucket in rows.items():
        for row in bucket.values():
            for parent_domain,parent_id in dependencies(row):
                if parent_id not in rows[parent_domain]:add('dependency_missing',domain,row.id)
            if hasattr(row,'available_at') and row.available_at is None:add('availability_unknown',domain,row.id,'gap')
    # Detect cycles iteratively so an arbitrary derived chain cannot bypass
    # closure verification or overflow the Python recursion limit.
    states={}
    for domain,bucket in rows.items():
        for identity in bucket:
            root=(domain,identity)
            if states.get(root)==2:continue
            pending=[(root,False)]
            while pending:
                key,leaving=pending.pop()
                if leaving:
                    states[key]=2
                    continue
                if states.get(key)==1:
                    add('dependency_cycle',key[0],key[1]);continue
                if states.get(key)==2:continue
                states[key]=1
                pending.append((key,True))
                row=rows[key[0]].get(key[1])
                if row:
                    for parent in dependencies(row):
                        if parent[1] in rows[parent[0]]:pending.append((parent,False))
    facts_by_document=defaultdict(list)
    for source in rows['source_financial_fact'].values():facts_by_document[source.document_id].append(source)
    for document in rows['financial_source_document'].values():
        facts=facts_by_document[document.id]
        for reason in financial_inventory(document,facts):
            add(reason,'financial_source_document',document.id,'gap' if reason=='financial_extraction_incomplete' else 'error')
        if not re.fullmatch('[a-f0-9]{64}',document.content_hash):add('document_hash_invalid','financial_source_document',document.id)
        filing=rows['filing'].get(document.filing_id)
        if filing and document.available_at<filing.available_at:add('document_publication_backdated','financial_source_document',document.id)
    for row in rows['source_financial_fact'].values():
        document=rows['financial_source_document'].get(row.document_id)
        filing=rows['filing'].get(document.filing_id) if document else None
        if filing and row.issuer_id!=filing.issuer_id:add('source_issuer_mismatch','source_financial_fact',row.id)
        if document and row.available_at<document.available_at:add('source_availability_backdated','source_financial_fact',row.id)
        if row.first_seen_at<row.captured_at or row.extracted_at<row.captured_at:add('source_processing_backdated','source_financial_fact',row.id)
    for decision in rows['selection_decision'].values():
        parent=rows.get(decision.record_domain,{}).get(decision.record_id)
        if parent:
            times=[getattr(parent,name,None) for name in ('captured_at','first_seen_at','extracted_at','materialized_at')]
            if any(value and decision.recorded_at<value for value in times):
                add('decision_processing_backdated','selection_decision',decision.id)
    for price in rows['price_observation'].values():
        if price.session>price.captured_at.astimezone(timezone.utc).date():
            add('price_session_after_capture','price_observation',price.id)
        if price.evidence.get('availability_precision')=='capture_only' and price.available_at<price.captured_at:
            add('capture_only_availability_backdated','price_observation',price.id)
        if daily:
            from ..prices.provider_basis import contract_price_basis
            contract=rows.get('source_contract',{}).get(price.source_contract_id)
            try:definition=contract_price_basis(contract)
            except (ValueError,TypeError,AttributeError):definition=None
            if definition is None or price.basis!=definition['basis']:
                add('supplier_price_basis_unqualified','price_observation',price.id,'gap')
            else:
                recorded=(price.evidence or {}).get('price_basis_policy')
                if recorded is not None and (not isinstance(recorded,dict) or any(
                        recorded.get(key)!=definition[key] for key in ('policy','basis','adjustments','volume_basis'))):
                    add('supplier_price_basis_unqualified','price_observation',price.id,'gap')
        elif price.basis!='as_traded':add('unsupported_price_basis','price_observation',price.id,'gap')
    for row in rows['derived_fact'].values():
        if not registered_definition(row):add('unsupported_metric_definition','derived_fact',row.id)
        if not valid_metric_state(row):add('metric_status_value_invalid','derived_fact',row.id)
        if row.value is not None and not row.input_refs:
            add('successful_derived_lineage_missing','derived_fact',row.id)
        public_metric=(query_version in {'release_query_v6','release_query_v7'} and row.basis.get('public_availability_policy')=='financial_public_pit_v1'
            or daily and row.basis.get('public_availability_policy')=='financial_daily_metadata_pit_v1')
        if public_metric and row.materialized_at<row.captured_at:add('derived_processing_backdated','derived_fact',row.id)
        for domain,identity in dependencies(row):
            parent=rows[domain].get(identity)
            when=getattr(parent,'available_at',None)
            if public_metric:
                physical=[getattr(parent,name,None) for name in ('captured_at','first_seen_at','extracted_at','materialized_at','recorded_at')]
                if any(when and row.materialized_at<when for when in physical):add('derived_processing_backdated','derived_fact',row.id)
                if daily and domain=='financial_daily_publication_evidence':
                    if parent is None or parent.status!='qualified' or parent.effective_available_at is None:
                        add('derived_public_input_unavailable','derived_fact',row.id)
                        continue
                    when=parent.effective_available_at
                    if row.available_at<when or row.evaluation_as_of<when:add('derived_future_input','derived_fact',row.id)
                    continue
                from .public_clock import row_availability
                from .query_context import QueryContext
                context=QueryContext('validation',row.evaluation_as_of,row.basis.get('history_mode','reconstructed_public'),
                    'fundamentals_sql','periodic_report',True)
                projected=row_availability(parent,rows,context) if parent else None
                if projected is not None:
                    when=projected['effective_available_at']
                    if projected['status']=='unavailable':add('derived_public_input_unavailable','derived_fact',row.id)
            if when and (row.available_at<when or row.evaluation_as_of<when):add('derived_future_input','derived_fact',row.id)
    for row in rows['fundamental_fact'].values():
        source=rows['source_financial_fact'].get(row.source_fact_id)
        if source and row.issuer_id!=source.issuer_id:add('canonical_issuer_mismatch','fundamental_fact',row.id)
        if source and row.available_at<source.available_at:add('canonical_source_availability_backdated','fundamental_fact',row.id)
    for raw in rows['raw_object'].values():
        reason=raw_issue(raw,raw_root)
        if reason:add(reason,'raw_object',raw.id)
    return issues


def frozen_rows(session,release,*,verify=True):
    if release.status!='sealed':raise ValueError('unsealed_release')
    if digest(release.manifest)!=release.content_hash:raise ValueError('release_manifest_hash_mismatch')
    descriptors={value['id']:value for value in release.manifest.get('segments',[])}
    links=session.scalars(select(ReleaseSegment).where(ReleaseSegment.release_id==release.id)).all()
    if set(descriptors)!={link.segment_id for link in links}:raise ValueError('release_segment_membership_hash_mismatch')
    rows={domain:{} for domain in MODELS}
    for link in sorted(links,key=lambda value:value.segment_id):
        segment=session.get(DataSegment,link.segment_id)
        descriptor=descriptors[link.segment_id]
        if segment is None or segment.domain!=descriptor['domain'] or segment.content_hash!=descriptor['content_hash']:
            raise ValueError('segment_descriptor_hash_mismatch')
        members=session.scalars(select(SegmentMember).where(SegmentMember.segment_id==segment.id).order_by(SegmentMember.record_id)).all()
        if len(members)!=descriptor['count']:raise ValueError('segment_count_hash_mismatch')
        domain=segment.domain
        if domain not in MODELS or any(member.record_domain!=domain for member in members):raise ValueError('undefined_segment_domain')
        ids={member.record_id for member in members}
        records=[]
        for batch in id_batches(ids):
            records.extend(session.scalars(select(MODELS[domain]).where(MODELS[domain].id.in_(batch)).order_by(MODELS[domain].id)
                                .execution_options(populate_existing=True)).all())
        if len(records)!=len(ids):raise ValueError('segment_row_missing')
        if verify and segment_digest(domain,records)!=segment.content_hash:raise ValueError('segment_row_hash_mismatch')
        rows[domain].update((row.id,row) for row in records)
    for domain,bucket in rows.items():
        for row in bucket.values():
            if any(identity not in rows[parent_domain] for parent_domain,identity in dependencies(row)):
                raise ValueError('frozen_dependency_closure_missing')
    return rows


def validate_dataset(database,raw_root):
    with database_session(database) as session:
        issues=[]
        releases=session.scalars(select(DatasetRelease).order_by(DatasetRelease.id)).all()
        counts={}
        for release in releases:
            try:
                rows=frozen_rows(session,release)
                issues.extend({**item,'release_id':release.id} for item in validate_rows(rows,raw_root,policies=release.manifest.get('policies')))
                counts[release.id]={domain:len(bucket) for domain,bucket in rows.items()}
            except ValueError as exc:issues.append({'release_id':release.id,'reason':str(exc),'severity':'error'})
        if not releases:
            rows={domain:{row.id:row for row in session.scalars(select(model))} for domain,model in MODELS.items()}
            issues.extend(validate_rows(rows,raw_root))
        blocked=any(item['severity']=='error' for item in issues)
        return {'status':'blocked' if blocked else 'research_partial','release_count':len(releases),'counts':counts,
                'issues':issues,'production_qualified':False,'financial_documents_required':False}
