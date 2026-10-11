"""Projection of verified document availability without rewriting physical clocks."""

from ..fundamentals.models import DerivedFact, FinancialSourceDocument, FundamentalFact, SourceFinancialFact
from ..fundamentals.public_availability import POLICY_VERSION, resolve_document_availability
from ..fundamentals.daily_publication import POLICY_VERSION as DAILY_POLICY, QUERY_VERSION as DAILY_QUERY, resolve_daily_availability


def public_clock_enabled(rows):
    return getattr(rows,'manifest',{}).get('policies',{}).get('query_version') in {'release_query_v6','release_query_v7',DAILY_QUERY}


def daily_clock_enabled(rows):
    return getattr(rows,'manifest',{}).get('policies',{}).get('query_version')==DAILY_QUERY


def _daily_unavailable(reason):
    return {'status':'unavailable','reason':reason,'effective_available_at':None,'metadata_public_bound':None,
        'evidence_ids':[],'precision':'unknown','policy_version':DAILY_POLICY,'evidence_grade':'metadata_grade',
        'original_archive_verified':False}


def _daily_cache(rows):
    # The index is release-local. Exact returned row identity is part of every
    # resolution key; document-level evidence cannot leak to another receipt.
    cache=getattr(rows,'daily_document_cache',None)
    if cache is None:
        evidence={}
        for proof in rows.get('financial_daily_publication_evidence',{}).values():
            evidence.setdefault(proof.document_id,[]).append(proof)
        cache={'evidence':evidence,'resolutions':{}}
        if hasattr(rows,'manifest'):rows.daily_document_cache=cache
    return cache


def _daily_row_availability(row,rows,context,*,_seen=None,_cache=None,_proof_scope=None):
    seen=set() if _seen is None else _seen
    key=(getattr(row,'__tablename__',type(row).__name__),row.id)
    if key in seen:return _daily_unavailable('availability_dependency_cycle')
    seen=seen|{key}
    cache=_daily_cache(rows) if _cache is None else _cache
    source=None
    if isinstance(row,FundamentalFact):
        source=rows.get('source_financial_fact',{}).get(row.source_fact_id)
        if source is None or source.issuer_id!=row.issuer_id:return _daily_unavailable('source_lineage_missing')
        return _daily_row_availability(source,rows,context,_seen=seen,_cache=cache,_proof_scope=_proof_scope)
    if isinstance(row,SourceFinancialFact):
        source=row
        document=rows.get('financial_source_document',{}).get(row.document_id)
    elif isinstance(row,FinancialSourceDocument):document=row
    else:document=None
    if document is not None:
        filing=rows.get('filing',{}).get(document.filing_id)
        if filing is None:return _daily_unavailable('filing_lineage_missing')
        if source is not None and source.issuer_id!=filing.issuer_id:return _daily_unavailable('source_issuer_mismatch')
        cache_key=(document.id,source.id if source is not None else None,context.as_of,context.history_mode,_proof_scope)
        if cache_key not in cache['resolutions']:
            proofs=cache['evidence'].get(document.id,())
            if _proof_scope is not None:proofs=[proof for proof in proofs if proof.id in _proof_scope]
            try:
                result=resolve_daily_availability(document,filing,proofs,source_fact=source,
                    history_mode=context.history_mode,as_of=context.as_of)
            except ValueError:
                result=_daily_unavailable('invalid_daily_publication_identity')
            cache['resolutions'][cache_key]={**result,'precision':'public_day' if result['status']=='qualified' else 'unknown'}
        return cache['resolutions'][cache_key]
    if isinstance(row,SourceFinancialFact):return _daily_unavailable('document_lineage_missing')
    if not isinstance(row,DerivedFact):return None
    if (row.basis or {}).get('public_availability_policy')!=DAILY_POLICY:
        return _daily_unavailable('daily_metric_policy_mismatch')
    pinned={ref.get('id',ref.get('record_id')) for ref in row.input_refs
        if ref.get('domain',ref.get('record_domain'))=='financial_daily_publication_evidence'}
    scope=frozenset(pinned) if _proof_scope is None else _proof_scope.intersection(pinned)
    if not scope or any(identity not in rows.get('financial_daily_publication_evidence',{}) for identity in scope):
        return _daily_unavailable('daily_metric_metadata_lineage_missing')
    results=[]
    for ref in row.input_refs:
        domain=ref.get('domain',ref.get('record_domain'))
        if domain in {'selection_decision','financial_daily_publication_evidence'}:continue
        parent=rows.get(domain,{}).get(ref.get('id',ref.get('record_id')))
        if parent is None:return _daily_unavailable('daily_metric_input_missing')
        result=_daily_row_availability(parent,rows,context,_seen=seen,_cache=cache,_proof_scope=scope)
        if result is None:
            from .market_clock import availability
            result=availability(parent,rows)
        if result is None:return _daily_unavailable('daily_metric_input_clock_unknown')
        results.append(result)
    if not results:return _daily_unavailable('daily_metric_inputs_missing')
    if any(value['effective_available_at'] is None for value in results):
        return _daily_unavailable('daily_metric_input_unavailable')
    bound=max(value['effective_available_at'] for value in results)
    qualified=all(value['status'] in {'qualified','market_reconstructed'} for value in results)
    public_bounds=[value['metadata_public_bound'] for value in results if value.get('metadata_public_bound') is not None]
    public_days=[value['public_day'] for value in results if value.get('public_day') is not None]
    return {'effective_available_at':bound,'status':'qualified' if qualified else 'unavailable',
        'metadata_public_bound':max(public_bounds) if public_bounds else None,
        'public_day':max(public_days) if public_days else None,'reason':'input_daily_metadata_availability',
        'evidence_ids':sorted({identity for value in results for identity in value.get('evidence_ids',())}),
        'precision':'public_day' if qualified else 'unknown','policy_version':DAILY_POLICY,
        'evidence_grade':'metadata_grade','original_archive_verified':False}


def _document_cache(rows):
    """QueryRows is a frozen release projection; keep caches local to it."""
    if getattr(rows,'manifest',{}).get('policies',{}).get('query_version')!='release_query_v7':return None
    cache=getattr(rows,'public_document_cache',None)
    if cache is None:
        evidence={}
        for proof in rows.get('financial_publication_evidence',{}).values():
            evidence.setdefault(proof.document_id,[]).append(proof)
        cache={'evidence':evidence,'resolutions':{}}
        rows.public_document_cache=cache
    return cache


def row_availability(row, rows, context, *, _seen=None,_cache=None,_proof_scope=None):
    """Resolve only the supplied frozen closure; unsupported lineage fails closed."""
    if daily_clock_enabled(rows):
        return _daily_row_availability(row,rows,context,_seen=_seen,_cache=_cache,_proof_scope=_proof_scope)
    seen=set() if _seen is None else _seen
    key=(getattr(row,'__tablename__',type(row).__name__),row.id)
    if key in seen:
        return {'status':'unavailable','reason':'availability_dependency_cycle','effective_available_at':None,
            'evidence_ids':[],'precision':'unknown','policy_version':POLICY_VERSION,'withdrawn':False}
    seen=seen|{key}
    cache=_document_cache(rows) if _cache is None else _cache
    if isinstance(row,SourceFinancialFact):
        document=rows.get('financial_source_document',{}).get(row.document_id)
        return row_availability(document,rows,context,_seen=seen,_cache=cache,_proof_scope=_proof_scope) if document else None
    if isinstance(row,FundamentalFact):
        source=rows.get('source_financial_fact',{}).get(row.source_fact_id)
        return row_availability(source,rows,context,_seen=seen,_cache=cache,_proof_scope=_proof_scope) if source else None
    if isinstance(row,FinancialSourceDocument):
        filing=rows.get('filing',{}).get(row.filing_id)
        if filing is None:return None
        if cache is not None:
            cache_key=(row.id,context.as_of,context.history_mode,_proof_scope)
            if cache_key not in cache['resolutions']:
                proofs=cache['evidence'].get(row.id,())
                if _proof_scope is not None:
                    proofs=[proof for proof in proofs if proof.status!='qualified' or proof.id in _proof_scope]
                cache['resolutions'][cache_key]=resolve_document_availability(row,filing,proofs,
                    history_mode=context.history_mode,as_of=context.as_of)
            return cache['resolutions'][cache_key]
        return resolve_document_availability(row,filing,rows.get('financial_publication_evidence',{}).values(),
            history_mode=context.history_mode,as_of=context.as_of)
    if isinstance(row,DerivedFact) and (row.basis or {}).get('public_availability_policy')==POLICY_VERSION:
        results=[]
        pinned={ref.get('id',ref.get('record_id')) for ref in row.input_refs
            if ref.get('domain',ref.get('record_domain'))=='financial_publication_evidence'}
        if cache is not None:
            # A nested metric cannot regain a qualified proof excluded by its
            # parent. Rejections and withdrawals always remain in scope.
            scope=frozenset(pinned) if _proof_scope is None else _proof_scope.intersection(pinned)
            projection=rows
        else:
            scope=None
            projection={**rows,'financial_publication_evidence':{identity:evidence
                for identity,evidence in rows.get('financial_publication_evidence',{}).items()
                if evidence.status!='qualified' or identity in pinned}}
        for ref in row.input_refs:
            domain=ref.get('domain',ref.get('record_domain'))
            if domain in {'selection_decision','financial_publication_evidence'}:continue
            identity=ref.get('id',ref.get('record_id'))
            parent=rows.get(domain,{}).get(identity)
            if parent is None:return None
            result=row_availability(parent,projection,context,_seen=seen,_cache=cache,_proof_scope=scope)
            if result is None:return None
            results.append(result)
        if not results:return None
        if any(value['effective_available_at'] is None for value in results):return None
        bound=max(value['effective_available_at'] for value in results)
        unavailable=any(value['status']=='unavailable' for value in results)
        qualified=all(value['status']=='qualified' for value in results)
        public_bounds=[value.get('verified_public_bound') for value in results]
        return {'effective_available_at':bound,'status':'unavailable' if unavailable else 'qualified' if qualified else 'capture_only',
            'verified_public_bound':max(public_bounds) if all(value is not None for value in public_bounds) else None,
            'reason':'input_public_availability','evidence_ids':sorted({identity for value in results for identity in value['evidence_ids']}),
            'precision':'public_day' if qualified else 'capture_only','policy_version':POLICY_VERSION,
            'withdrawn':any(value.get('withdrawn',False) for value in results)}
    return None


def availability_payload(row,rows,context):
    if not public_clock_enabled(rows):return {}
    result=row_availability(row,rows,context)
    return {'public_availability':result} if result is not None else {}
