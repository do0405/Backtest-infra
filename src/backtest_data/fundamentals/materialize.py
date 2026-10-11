"""Transactional, append-only canonical mappings and audited pure metric outputs."""

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import hashlib

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, defer

from ..db import new_id, utcnow
from ..releases.models import SelectionDecision
from ..releases.query_context import definition_registry, definition_registry_hash,policy_bundle
from .domain import FinancialFact, SourceFact, aware, stable_hash
from .mapping_catalog import MappingCatalog, default_catalog, map_source_fact
from .models import DerivedFact, Filing, FinancialSourceDocument, FundamentalFact, SourceFinancialFact
from .numeric import NUMERIC_POLICY
from .ratios import calculate_metric
from .selection import AmbiguousFinancialRevision, select_facts_asof,select_public_facts_asof
from .storage import json_value


@dataclass
class CanonicalSummary:
    mapping_version: str
    mapping_hash: str
    scanned: int = 0
    created: int = 0
    reused: int = 0
    unmapped: int = 0
    unavailable: int = 0
    ineligible: int = 0
    reasons: dict[str, int] = field(default_factory=dict)


@dataclass
class MetricSummary:
    as_of: str
    history_mode: str
    registry_hash: str
    issuers: int = 0
    canonical_inputs: int = 0
    attempted: int = 0
    successful: int = 0
    unavailable: int = 0
    created: int = 0
    reused: int = 0
    skipped_no_lineage: int = 0
    skipped_cutoff: int = 0
    reasons: dict[str, int] = field(default_factory=dict)


@contextmanager
def _write_session(database):
    if isinstance(database, Session):
        if database.connection().get_isolation_level() != 'READ COMMITTED':
            raise ValueError('read_committed_materialization_session_required')
        with database.begin_nested():
            yield database
        return
    # READ COMMITTED is required: a waiter must see rows inserted by the lock holder.
    with database.connect().execution_options(isolation_level='READ COMMITTED') as connection:
        with Session(connection) as session, session.begin():
            yield session


def _validate_request(materialized_at, batch_size):
    aware(materialized_at, 'materialized_at')
    if materialized_at is None or not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise ValueError('aware_materialization_clock_and_positive_batch_size_required')


def _source_period_end(row):
    if row.period_kind!='instant':return row.period_end
    if row.period_end is not None and row.instant is not None and row.period_end!=row.instant:
        raise ValueError('conflicting_instant_source_dates')
    return row.instant or row.period_end


def _source_adapter(row):
    evidence = row.evidence or {}
    return SourceFact(id=row.id, concept=row.concept, taxonomy=evidence.get('taxonomy', ''), context_id=row.context_id,
        value=row.value, raw_value=row.raw_value or '', unit=row.unit, original_unit=row.original_unit,
        account_name=row.account_name, dimensions=row.dimensions, period_kind=row.period_kind,
        period_start=row.period_start, period_end=_source_period_end(row), scope=row.scope, decimals=row.decimals,
        precision=evidence.get('precision'), scale=row.scale, sign=evidence.get('sign'), is_nil=row.is_nil,
        format=evidence.get('format'), parse_status=evidence.get('parse_status', 'unknown'), evidence=evidence)


def _latest_decisions(session, domain, ids, *, as_of=None, history_mode='reconstructed_public'):
    if not ids:
        return {}
    query = select(SelectionDecision).where(SelectionDecision.record_domain == domain, SelectionDecision.record_id.in_(ids))
    if history_mode == 'observed_system':
        query = query.where(SelectionDecision.recorded_at <= as_of)
    decisions = {}
    for row in session.scalars(query.order_by(SelectionDecision.recorded_at, SelectionDecision.id)):
        decisions[row.record_id] = row
    return decisions


def _decision(session, row, rule_version, reason, recorded_at, *, domain='fundamental_fact'):
    existing = session.scalar(select(SelectionDecision.id).where(SelectionDecision.record_domain == domain,
        SelectionDecision.record_id == row.id, SelectionDecision.rule_version == rule_version))
    if not existing:
        session.add(SelectionDecision(record_domain=domain, record_id=row.id, status='accepted',
            rule_version=rule_version, evidence_grade='research_only', reason=reason,
            actor='financial_materializer', recorded_at=recorded_at))


def materialize_canonical(database, *, catalog: MappingCatalog | None = None,
                          materialized_at: datetime | None = None, batch_size: int = 500,
                          issuer_ids=None) -> CanonicalSummary:
    """Map every SQL source by default; unsupported source evidence stays intact."""
    catalog = catalog or default_catalog()
    explicit_clock = materialized_at is not None
    materialized_at = materialized_at or utcnow()
    _validate_request(materialized_at, batch_size)
    report = CanonicalSummary(catalog.version, catalog.content_hash)
    reasons = Counter()
    with _write_session(database) as session:
        key = int.from_bytes(hashlib.sha256(('mapping:' + catalog.version).encode()).digest()[:8], 'big', signed=True)
        session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})
        hashes = session.scalars(select(FundamentalFact.basis['mapping_hash'].astext).where(
            FundamentalFact.mapping_version == catalog.version).distinct())
        if any(mapping_hash != catalog.content_hash for mapping_hash in hashes):
            raise ValueError('mapping_version_hash_conflict')
        last = ''
        while True:
            query = select(SourceFinancialFact).where(SourceFinancialFact.id > last).order_by(SourceFinancialFact.id).limit(batch_size)
            if issuer_ids is not None:
                query = query.where(SourceFinancialFact.issuer_id.in_(tuple(issuer_ids)))
            sources = session.scalars(query.with_for_update()).all()
            if not sources:
                break
            last = sources[-1].id
            source_ids = [source.id for source in sources]
            documents = {row.id: row for row in session.scalars(select(FinancialSourceDocument).options(
                defer(FinancialSourceDocument.inventory)).where(
                FinancialSourceDocument.id.in_({source.document_id for source in sources})))}
            filings = {row.id: row for row in session.scalars(select(Filing).where(
                Filing.id.in_({document.filing_id for document in documents.values()})))}
            decisions = _latest_decisions(session, 'source_financial_fact', source_ids)
            existing = {row.source_fact_id: row for row in session.scalars(select(FundamentalFact).where(
                FundamentalFact.source_fact_id.in_(source_ids), FundamentalFact.mapping_version == catalog.version))}
            for source in sources:
                report.scanned += 1
                document = documents.get(source.document_id)
                filing = filings.get(document.filing_id) if document else None
                decision = decisions.get(source.id)
                if (document is None or filing is None or filing.issuer_id != source.issuer_id
                    or not document.extraction_complete or not decision or decision.status != 'accepted'):
                    report.ineligible += 1
                    reasons['source_extraction_or_adoption_ineligible'] += 1
                    continue
                input_clock = max(source.available_at, source.captured_at, source.first_seen_at, source.extracted_at,
                    document.available_at, document.captured_at, filing.available_at, filing.captured_at, decision.recorded_at)
                if materialized_at < input_clock:
                    if explicit_clock:
                        raise ValueError('materialization_precedes_input_clock')
                    report.ineligible += 1
                    reasons['input_newer_than_scan_cutoff'] += 1
                    continue
                if source.id in existing:
                    report.reused += 1
                    continue
                normalized = map_source_fact(_source_adapter(source), catalog=catalog,
                    accounting_basis=document.accounting_basis, available_at=max(source.available_at, document.available_at, filing.available_at),
                    captured_at=max(source.captured_at, document.captured_at, filing.captured_at))
                if normalized.fact is None:
                    if normalized.status == 'unavailable':
                        report.unavailable += 1
                    else:
                        report.unmapped += 1
                    reasons[normalized.reason or normalized.status] += 1
                    continue
                fact = normalized.fact
                completed_at = materialized_at if explicit_clock else utcnow()
                if completed_at < input_clock:
                    raise ValueError('materialization_precedes_input_clock')
                row = FundamentalFact(issuer_id=source.issuer_id, source_fact_id=source.id, metric_id=fact.metric,
                    mapping_version=catalog.version, value=fact.value, unit=fact.unit,
                    period=json_value({'kind': source.period_kind, 'start': source.period_start, 'end': source.period_end,
                                       'instant': source.instant}),
                    basis=json_value({**fact.evidence, 'scope': source.scope, 'currency': fact.currency,
                        'accounting_basis': document.accounting_basis, 'share_basis': None, 'fiscal_lineage': None,
                        'dimensions': source.dimensions, 'sign_policy': fact.sign_policy, 'directly_reported': True,
                        'source_fact_signature': source.signature, 'source_document_id': document.id,
                        'source_document_hash': document.content_hash, 'source_parser_version': document.parser_version,
                        'disclosure_profile': document.disclosure_profile,
                        'source_decision_id': decision.id, 'source_first_seen_at': source.first_seen_at,
                        'source_extracted_at': source.extracted_at, 'mapping_catalog': catalog.policy,
                        'processing_clock_policy': 'explicit_clock' if explicit_clock else 'post_normalization_row_clock_v1'}),
                    available_at=fact.available_at, captured_at=fact.captured_at, materialized_at=completed_at)
                session.add(row)
                session.flush()
                _decision(session, row, catalog.version,
                    'Exact standard concept/unit mapping; source scope, accounting and fiscal qualifications remain as recorded', completed_at)
                report.created += 1
            session.flush()
    report.reasons = dict(sorted(reasons.items()))
    return report


def _date(value, field_name, *, optional=False):
    if value is None and optional:
        return None
    try:
        return value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError(f'invalid_canonical_{field_name}') from None


def canonical_to_financial_fact(row: FundamentalFact, *, source: SourceFinancialFact | None = None,
                                decision: SelectionDecision | None = None, availability=None) -> FinancialFact:
    """Adapt explicit metadata. Duration length alone never establishes a quarter."""
    basis, period = row.basis or {}, row.period or {}
    if not isinstance(basis, dict) or not isinstance(period, dict):
        raise ValueError('invalid_canonical_metadata')
    if source is not None and (source.id != row.source_fact_id or source.issuer_id != row.issuer_id):
        raise ValueError('source_lineage_missing')
    kind = period.get('kind', period.get('period_kind', 'unknown'))
    if kind not in {'unknown', 'duration', 'instant', 'quarter', 'ytd', 'annual', 'ttm'}:
        raise ValueError('invalid_canonical_period_kind')
    fiscal = {key: period.get(key) for key in ('fiscal_year', 'fiscal_quarter', 'quarter_index', 'fiscal_cycle')}
    lineage = basis.get('fiscal_lineage')
    qualification = basis.get('qualification_evidence', ())
    if not isinstance(qualification, (list, tuple)) or any(not isinstance(value, str) or not value.strip() for value in qualification):
        raise ValueError('invalid_canonical_qualification_evidence')
    for name in ('directly_reported', 'period_compatible'):
        if not isinstance(basis.get(name, True), bool):
            raise ValueError(f'invalid_canonical_{name}')
    revision = basis.get('revision', 0)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError('invalid_canonical_revision')
    components = basis.get('component_ids', ())
    if not isinstance(components, (list, tuple)) or any(not isinstance(value, str) or not value for value in components):
        raise ValueError('invalid_canonical_component_ids')
    if lineage is not None and not isinstance(lineage, str):
        raise ValueError('invalid_canonical_fiscal_lineage')
    if kind in {'quarter', 'ytd', 'annual', 'ttm'}:
        if (not isinstance(lineage, str) or not lineage or lineage == 'unknown'
                or not qualification or not isinstance(fiscal['fiscal_cycle'], str) or not fiscal['fiscal_cycle']
                or not isinstance(fiscal['fiscal_year'], int) or isinstance(fiscal['fiscal_year'], bool)):
            raise ValueError('explicit_fiscal_qualification_required')
        if kind in {'quarter', 'ytd'} and (
            not isinstance(fiscal['fiscal_quarter'], int) or isinstance(fiscal['fiscal_quarter'], bool)
            or fiscal['fiscal_quarter'] not in {1, 2, 3, 4}
            or not isinstance(fiscal['quarter_index'], int) or isinstance(fiscal['quarter_index'], bool)):
            raise ValueError('invalid_explicit_fiscal_period')
    elif any(value is not None for value in fiscal.values()) or lineage not in {None, 'unknown'}:
        # Instant balances may carry an explicitly verified fiscal lineage.
        if kind != 'instant' or not basis.get('qualification_evidence'):
            raise ValueError('explicit_fiscal_qualification_required')
    scope = basis.get('scope', 'unknown')
    accounting = basis.get('accounting_basis')
    if accounting is not None and (not isinstance(accounting, str) or accounting not in {'', 'unknown', 'US_GAAP', 'IFRS', 'K_IFRS', 'K_GAAP'}):
        raise ValueError('invalid_canonical_accounting_basis')
    accounting = None if accounting in {None, '', 'unknown'} else accounting
    if not isinstance(scope, str) or scope not in {'unknown', 'consolidated', 'separate', 'parent', 'parent_common'}:
        raise ValueError('invalid_canonical_scope')
    start = _date(period.get('start', period.get('period_start')), 'period_start', optional=True)
    end = _date(period.get('end') or period.get('period_end') or period.get('instant'), 'period_end')
    if start and start > end:
        raise ValueError('invalid_canonical_period_order')
    if kind in {'quarter', 'annual', 'ytd', 'ttm'} and start is None:
        raise ValueError('invalid_canonical_period_start')
    if source is not None and (start != source.period_start or end != _source_period_end(source)):
        raise ValueError('canonical_source_period_mismatch')
    currency = basis.get('currency')
    if currency is not None and (not isinstance(currency, str) or currency not in default_catalog().currencies):
        raise ValueError('invalid_canonical_currency')
    if currency is not None and row.unit not in {currency, currency + '/share', currency + '/shares'}:
        raise ValueError('canonical_currency_unit_mismatch')
    dimensions = basis.get('dimensions', {})
    if not isinstance(dimensions, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in dimensions.items()):
        raise ValueError('invalid_canonical_dimensions')
    share_basis = basis.get('share_basis')
    if share_basis is not None and not isinstance(share_basis, str):
        raise ValueError('invalid_canonical_share_basis')
    share_basis = None if share_basis in {None, '', 'unknown'} else share_basis
    return FinancialFact(id=row.id, metric=row.metric_id, value=row.value, period_kind=kind,
        period_start=start, period_end=end, unit=row.unit, scope=scope, currency=currency, accounting_basis=accounting,
        available_at=availability['effective_available_at'] if availability else row.available_at, captured_at=row.captured_at, materialized_at=row.materialized_at,
        source_record_ids=(row.id,), fiscal_lineage=lineage if lineage != 'unknown' else None,
        fiscal_year=fiscal['fiscal_year'], fiscal_quarter=fiscal['fiscal_quarter'], quarter_index=fiscal['quarter_index'],
        fiscal_cycle=fiscal['fiscal_cycle'], share_basis=share_basis,
        directly_reported=basis.get('directly_reported', True), period_compatible=basis.get('period_compatible', True),
        first_seen_at=source.first_seen_at if source else None, extracted_at=source.extracted_at if source else None,
        decision_recorded_at=decision.recorded_at if decision else None,
        dimensions=tuple(sorted(dimensions.items())), sign_policy=basis.get('sign_policy'),
        component_ids=tuple(components), revision=revision,
        evidence={**basis, 'numeric_policy': NUMERIC_POLICY,**({'public_availability':availability} if availability else {})})


VALUATION_METRICS = {'market_cap_common_pit', 'per_diluted_ttm', 'per_diluted_fy', 'pbr_common',
                     'enterprise_value_book_basis', 'ev_ebitda_ttm'}


def materialize_metrics(database, *, as_of: datetime, history_mode: str = 'reconstructed_public',
                        materialized_at: datetime | None = None, batch_size: int = 500,
                        issuer_ids=None, metric_ids=None,query_version='release_query_v5') -> MetricSummary:
    """Evaluate all registered pure financial metrics on eligible SQL canonical inputs."""
    aware(as_of, 'as_of')
    policies=policy_bundle(query_version)
    daily_pit=query_version=='release_query_v8_daily'
    public_pit=query_version in {'release_query_v6','release_query_v7','release_query_v8_daily'}
    explicit_clock = materialized_at is not None
    materialized_at = materialized_at or utcnow()
    _validate_request(materialized_at, batch_size)
    if as_of is None or history_mode not in {'reconstructed_public', 'observed_system'}:
        raise ValueError('explicit_as_of_and_supported_history_mode_required')
    as_of = as_of.astimezone(timezone.utc)
    registry = definition_registry()
    supported = set(registry) - VALUATION_METRICS
    metrics = tuple(sorted(supported if metric_ids is None else set(metric_ids)))
    if set(metrics) - supported:
        raise ValueError('unsupported_pure_financial_metric')
    report = MetricSummary(as_of.isoformat(), history_mode, definition_registry_hash())
    reasons = Counter()
    with _write_session(database) as session:
        issuer_query = select(FundamentalFact.issuer_id).distinct().order_by(FundamentalFact.issuer_id)
        if issuer_ids is not None:
            issuer_query = issuer_query.where(FundamentalFact.issuer_id.in_(tuple(issuer_ids)))
        # Issuer IDs are small; canonical/source rows are loaded one issuer at a time.
        for issuer_id in session.scalars(issuer_query).all():
            query=select(FundamentalFact).where(FundamentalFact.issuer_id==issuer_id)
            if not public_pit:query=query.where(FundamentalFact.available_at<=as_of)
            canonical = session.scalars(query.order_by(FundamentalFact.id)).all()
            if not canonical:
                continue
            sources = {row.id: row for row in session.scalars(select(SourceFinancialFact).where(
                SourceFinancialFact.id.in_({row.source_fact_id for row in canonical})))}
            documents = {row.id: row for row in session.scalars(select(FinancialSourceDocument).options(
                defer(FinancialSourceDocument.inventory)).where(
                FinancialSourceDocument.id.in_({source.document_id for source in sources.values()})))}
            filings = {row.id: row for row in session.scalars(select(Filing).where(
                Filing.id.in_({document.filing_id for document in documents.values()})))}
            evidence=[]
            if public_pit:
                if daily_pit:
                    from .daily_publication_models import FinancialDailyPublicationEvidence
                    from .daily_publication import resolve_daily_availability,select_daily_facts_asof
                    evidence=session.scalars(select(FinancialDailyPublicationEvidence).where(FinancialDailyPublicationEvidence.document_id.in_(documents))).all()
                else:
                    from .publication_models import FinancialPublicationEvidence
                    from .public_availability import resolve_document_availability
                    evidence=session.scalars(select(FinancialPublicationEvidence).where(FinancialPublicationEvidence.document_id.in_(documents))).all()
            source_decisions = _latest_decisions(session, 'source_financial_fact', tuple(sources), as_of=as_of, history_mode=history_mode)
            canonical_decisions = _latest_decisions(session, 'fundamental_fact', [row.id for row in canonical], as_of=as_of, history_mode=history_mode)
            eligible = []
            refs_by_id = {}
            clocks = {}
            profiles = {}
            resolutions={}
            for row in canonical:
                source = sources.get(row.source_fact_id)
                if source is None:
                    raise ValueError('source_lineage_missing')
                document = documents.get(source.document_id)
                filing = filings.get(document.filing_id) if document else None
                if document is None or filing is None or filing.issuer_id != row.issuer_id:
                    raise ValueError('source_lineage_missing')
                if document.disclosure_profile != 'periodic_report':
                    reasons['unsupported_disclosure_profile'] += 1
                    continue
                source_decision = source_decisions.get(source.id)
                canonical_decision = canonical_decisions.get(row.id)
                if (not document.extraction_complete or source_decision is None or canonical_decision is None
                    or source_decision.status != 'accepted' or canonical_decision.status != 'accepted'):
                    reasons['input_adoption_ineligible'] += 1
                    continue
                resolution=(resolve_daily_availability(document,filing,evidence,source_fact=source,history_mode=history_mode,as_of=as_of)
                    if daily_pit else resolve_document_availability(document,filing,evidence,history_mode=history_mode,as_of=as_of) if public_pit else None)
                if (resolution is not None and resolution['status']=='unavailable'):
                    reasons[resolution['reason']]+=1
                    continue
                if query_version=='release_query_v7' and (resolution is None or resolution['status']!='qualified'):
                    reasons['public_availability_certificate_required']+=1
                    continue
                if query_version=='release_query_v7':
                    from .completion import original_metadata_qualified
                    if not original_metadata_qualified(row.basis,evidence,resolution):
                        reasons['semantic_public_version_unqualified']+=1
                        continue
                if ((not public_pit or history_mode=='observed_system') and any(record.available_at > as_of for record in (row,source, document, filing))
                    or _source_period_end(source) and _source_period_end(source) > as_of.astimezone(timezone.utc).date()):
                    reasons['future_source_input'] += 1
                    continue
                processing = max(row.materialized_at, row.captured_at, source.captured_at, source.first_seen_at,
                    source.extracted_at, document.captured_at, filing.captured_at, source_decision.recorded_at, canonical_decision.recorded_at)
                if resolution and resolution['evidence_ids']:
                    selected_evidence=[item for item in evidence if item.id in resolution['evidence_ids']]
                    processing=max(processing,*(max(item.captured_at,item.recorded_at) for item in selected_evidence))
                if history_mode == 'observed_system' and processing > as_of:
                    reasons['future_or_unobserved_input'] += 1
                    continue
                if materialized_at < processing:
                    if explicit_clock:
                        raise ValueError('materialization_precedes_input_clock')
                    reasons['input_newer_than_scan_cutoff'] += 1
                    continue
                try:
                    fact = canonical_to_financial_fact(row, source=source, decision=canonical_decision,availability=resolution)
                except ValueError as exc:
                    reasons[str(exc)] += 1
                    continue
                eligible.append(fact)
                refs_by_id[row.id] = [
                    {'domain': 'fundamental_fact', 'id': row.id}, {'domain': 'source_financial_fact', 'id': source.id},
                    {'domain': 'selection_decision', 'id': source_decision.id}, {'domain': 'selection_decision', 'id': canonical_decision.id}]
                if resolution:
                    refs_by_id[row.id].extend({'domain':'financial_daily_publication_evidence' if daily_pit else 'financial_publication_evidence','id':identity} for identity in resolution['evidence_ids'])
                    resolutions[row.id]=resolution
                clocks[row.id] = (resolution['effective_available_at'] if resolution else max(row.available_at, source.available_at, document.available_at, filing.available_at),
                    max(row.captured_at, source.captured_at, document.captured_at, filing.captured_at))
                profiles[row.id] = document.disclosure_profile
            report.issuers += 1
            report.canonical_inputs += len(eligible)
            try:
                selector=select_daily_facts_asof if daily_pit else select_public_facts_asof if public_pit else select_facts_asof
                selected = selector(eligible, as_of=as_of, history_mode=history_mode)
            except AmbiguousFinancialRevision:
                reasons['ambiguous_financial_revision'] += len(metrics)
                report.attempted += len(metrics)
                report.unavailable += len(metrics)
                report.skipped_no_lineage += len(metrics)
                continue
            if query_version=='release_query_v7':
                from .completion import complete_quarter_inputs
                selected=complete_quarter_inputs(selected,as_of=as_of,history_mode=history_mode,materialized_at=materialized_at)
            for metric_id in metrics:
                report.attempted += 1
                computed = calculate_metric(metric_id, selected, as_of=as_of, history_mode=history_mode, materialized_at=materialized_at)
                completed_at = materialized_at if explicit_clock else utcnow()
                if history_mode == 'observed_system' and completed_at > as_of:
                    report.unavailable += 1
                    reasons['materialization_after_cutoff'] += 1
                    report.skipped_cutoff += 1
                    continue
                if computed.status == 'ok' and computed.value is not None:
                    report.successful += 1
                else:
                    report.unavailable += 1
                    reasons[computed.reason or computed.status] += 1
                if not computed.input_record_ids:
                    if computed.value is not None:
                        raise ValueError('successful_derived_lineage_missing')
                    report.skipped_no_lineage += 1
                    continue
                if any(identity not in refs_by_id for identity in computed.input_record_ids):
                    raise ValueError('undefined_metric_input_lineage')
                input_profiles = {profiles[identity] for identity in computed.input_record_ids}
                if input_profiles != {'periodic_report'}:
                    raise ValueError('unsupported_or_mixed_disclosure_profile')
                refs = [ref for identity in computed.input_record_ids for ref in refs_by_id[identity]]
                refs = [dict(pair) for pair in sorted({tuple(sorted(ref.items())) for ref in refs})]
                definition = registry[metric_id]
                signature_basis={'entity_id': issuer_id, 'metric': metric_id, 'definition': definition,
                    'input_refs': refs, 'as_of': as_of, 'history_mode': history_mode, 'numeric_policy': NUMERIC_POLICY}
                public_basis={}
                if public_pit:
                    public_basis={'query_version':query_version,'public_availability_policy':policies['public_availability_policy'],
                        'input_public_availability':{identity:resolutions[identity] for identity in computed.input_record_ids}}
                    signature_basis.update(public_basis)
                signature = stable_hash(signature_basis)
                available = max(clocks[identity][0] for identity in computed.input_record_ids)
                captured = max(clocks[identity][1] for identity in computed.input_record_ids)
                values = dict(id=new_id(), entity_id=issuer_id, entity_type='issuer', metric_id=metric_id,
                    definition_version=definition['definition_version'], definition_hash=definition['definition_hash'],
                    signature=signature, value=computed.value, unit=computed.unit, status=computed.status,
                    reason=computed.reason, period=json_value({key: value for key, value in computed.evidence.items()
                        if key in {'period_kind', 'period_start', 'period_end', 'current_period_end', 'previous_period_end'}}),
                    basis=json_value({**computed.evidence,**public_basis, 'numeric_policy': NUMERIC_POLICY, 'scope': computed.scope,
                        'currency': computed.currency, 'share_basis': computed.share_basis, 'history_mode': history_mode,
                        'disclosure_profile': next(iter(input_profiles)),
                        'definition_registry_hash': report.registry_hash, 'definition': definition,
                        'flags': computed.flags, 'absolute_delta': computed.absolute_delta}), input_refs=refs,
                    available_at=available, captured_at=captured, materialized_at=completed_at, evaluation_as_of=as_of)
                inserted = session.scalar(insert(DerivedFact).values(**values).on_conflict_do_nothing(
                    index_elements=[DerivedFact.signature]).returning(DerivedFact.id))
                if inserted:
                    report.created += 1
                    row = session.get(DerivedFact, inserted)
                    _decision(session, row, 'derived_metric_policy_v1',
                        'Registered pure financial definition with explicit SQL input and selection decision closure',
                        completed_at, domain='derived_fact')
                else:
                    report.reused += 1
            session.flush()
    report.reasons = dict(sorted(reasons.items()))
    return report
