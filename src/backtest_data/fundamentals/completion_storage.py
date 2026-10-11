"""Bounded additive SQL financial qualification and durable unavailability audit."""
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
import hashlib

from sqlalchemy import Integer, func, or_, select
from sqlalchemy.orm import aliased

from ..db import utcnow
from ..releases.models import SelectionDecision, ValidationIssue
from .completion import COMPLETION_POLICY, METADATA_POLICY, parse_sec_xbrl, qualify_source_fact
from .domain import aware, stable_hash
from .mapping_catalog import default_catalog, map_source_fact
from .materialize import _latest_decisions, _source_adapter, _write_session
from .models import Filing, FinancialSourceDocument, FundamentalFact, SourceFinancialFact
from .storage import json_value

AUDIT_SCHEMA = 'financial_input_gap_audit_v1'
QUALIFICATION_KEYS = ('scope', 'accounting_basis', 'share_basis', 'fiscal_lineage',
                      'qualification_evidence', 'directly_reported', 'sec_fiscal_metadata')


def _semantic_signature(source_signature, period, basis, document_hash, mapping_hash):
    return stable_hash({'source': source_signature, 'qualification': {
        'period': json_value(period), 'basis': {key: basis[key] for key in QUALIFICATION_KEYS if key in basis}},
        'document_hash': document_hash, 'mapping_hash': mapping_hash})


def _metric_gap_catalog(reasons):
    from ..releases.query_context import definition_registry
    from .materialize import VALUATION_METRICS
    common = sorted(reason for reason in reasons if reason not in {
        'unsupported_dimensions_or_scope', 'unsupported_taxonomy_or_concept',
        'missing_share_basis', 'no_additional_semantic_evidence'})
    result = {}
    for metric in sorted(definition_registry()):
        gaps = common + (['missing_share_basis'] if 'eps_' in metric and reasons.get('missing_share_basis') else [])
        if metric in VALUATION_METRICS:
            gaps.append('price_share_class_lineage_unavailable')
        result[metric] = {'status': 'document_input_unqualified' if gaps else 'requires_issuer_evaluation',
                          'reasons': gaps}
    return result


@dataclass
class CompletionSummary:
    policy: str = COMPLETION_POLICY
    documents: int = 0
    scanned: int = 0
    mapped: int = 0
    fiscal_qualified: int = 0
    created: int = 0
    reused: int = 0
    audited: int = 0
    next_after_document_id: str | None = None
    reasons: dict[str, int] = field(default_factory=dict)


def _lock_document(session, document_id):
    # Reuse the source document lock for supplement/materialization idempotency.
    return session.scalar(select(FinancialSourceDocument).where(
        FinancialSourceDocument.id == document_id).with_for_update())


def _annotation(session, *, document_id, rule, evidence, created_at, severity='warning'):
    signature = stable_hash(evidence)
    existing = session.scalar(select(ValidationIssue).where(
        ValidationIssue.record_domain == 'financial_source_document', ValidationIssue.record_id == document_id,
        ValidationIssue.rule == rule, ValidationIssue.evidence['signature'].astext == signature))
    if existing:
        return existing, False
    row = ValidationIssue(record_domain='financial_source_document', record_id=document_id,
        rule=rule, severity=severity, evidence=json_value({**evidence, 'signature': signature}), created_at=created_at)
    session.add(row)
    session.flush()
    return row, True


def supplement_sec_metadata(session, *, document_id: str, content: bytes, captured_at: datetime):
    """Reparse identical bytes in memory; add metadata without mutating old facts.

    The caller fetches the existing document URL. A different content hash is a
    different revision and cannot supply fiscal metadata to the stored version.
    Only six DEI fields are stored, never the filing body. Physical source clocks
    stay unchanged; the assertion has its own capture and recording clock.
    """
    aware(captured_at, 'captured_at')
    if captured_at is None:
        raise ValueError('supplement_capture_clock_required')
    document = _lock_document(session, document_id)
    filing = session.get(Filing, document.filing_id) if document else None
    if document is None or filing is None or filing.source != 'sec':
        raise ValueError('supplement_sec_document_required')
    if hashlib.sha256(content).hexdigest() != document.content_hash:
        raise ValueError('supplement_document_hash_mismatch')
    parsed = parse_sec_xbrl(content, form=filing.form)
    if not parsed.facts:
        raise ValueError('supplement_numeric_instance_required')
    metadata = parsed.facts[0].evidence['sec_fiscal_metadata']
    recorded_at = utcnow()
    if recorded_at < captured_at:
        raise ValueError('supplement_recording_precedes_capture')
    evidence = {'document_hash': document.content_hash, 'metadata': metadata,
        'source': 'hash_identical_document_refetch',
        'historical_public_availability_qualified': False}
    signature = stable_hash(evidence)
    existing = session.scalar(select(ValidationIssue).where(
        ValidationIssue.record_domain == 'financial_source_document', ValidationIssue.record_id == document_id,
        ValidationIssue.rule == METADATA_POLICY, ValidationIssue.evidence['signature'].astext == signature))
    if existing:
        return existing
    row = ValidationIssue(record_domain='financial_source_document', record_id=document_id,
        rule=METADATA_POLICY, severity='info' if metadata['status'] == 'reported' else 'warning',
        evidence=json_value({**evidence, 'signature': signature, 'captured_at': captured_at}), created_at=recorded_at)
    session.add(row)
    session.flush()
    return row


def materialize_financial_completion(database, *, document_ids=None, max_documents=100,
                                    after_document_id='', max_source_facts=50000, only_pending=False) -> CompletionSummary:
    """Map one bounded document page and persist document-level missing reasons.

    This is deliberately separate from source publication certification. Passing
    explicit document IDs is recommended during concurrent collection. The
    default keyset cursor permits small, resumable batches without a fact-table
    sweep. Each canonical row retains its original physical availability.
    """
    if (type(max_documents) is not int or not 1 <= max_documents <= 1000
            or type(max_source_facts) is not int or not 1 <= max_source_facts <= 200000):
        raise ValueError('bounded_completion_limits_required')
    if document_ids is not None:
        document_ids = tuple(dict.fromkeys(document_ids))
        if len(document_ids) > max_documents:
            raise ValueError('explicit_documents_exceed_completion_bound')
    report = CompletionSummary()
    reasons = Counter()
    catalog = default_catalog()
    mapping_hash = stable_hash({'catalog_hash': catalog.content_hash, 'completion_policy': COMPLETION_POLICY,
                                'metadata_policy': METADATA_POLICY})
    with _write_session(database) as session:
        query = select(FinancialSourceDocument.id).where(FinancialSourceDocument.id > after_document_id)
        if only_pending:
            annotation = aliased(ValidationIssue)
            audit = aliased(ValidationIssue)
            latest_metadata = select(func.max(annotation.created_at)).where(
                annotation.record_domain == 'financial_source_document',
                annotation.record_id == FinancialSourceDocument.id,
                annotation.rule == METADATA_POLICY).correlate(FinancialSourceDocument).scalar_subquery()
            covered = select(audit.id).where(audit.record_domain == 'financial_source_document',
                audit.record_id == FinancialSourceDocument.id, audit.rule == COMPLETION_POLICY,
                audit.evidence['document_hash'].astext == FinancialSourceDocument.content_hash,
                audit.evidence['audit_schema'].astext == AUDIT_SCHEMA,
                or_(audit.evidence['source_budget_deferred'].astext == 'false',
                    audit.evidence['max_source_facts'].astext.cast(Integer) >= max_source_facts),
                or_(latest_metadata.is_(None), audit.created_at >= latest_metadata))
            query = query.where(~covered.exists())
        if document_ids is not None:
            query = query.where(FinancialSourceDocument.id.in_(document_ids))
        identities = session.scalars(query.order_by(FinancialSourceDocument.id).limit(max_documents)).all()
        for document_id in identities:
            document = _lock_document(session, document_id)
            filing = session.get(Filing, document.filing_id)
            report.documents += 1
            report.next_after_document_id = document_id
            local_reasons = Counter()
            sources = session.scalars(select(SourceFinancialFact).where(SourceFinancialFact.document_id == document_id)
                .order_by(SourceFinancialFact.id).limit(max_source_facts + 1)).all()
            metadata_rows = session.scalars(select(ValidationIssue).where(
                ValidationIssue.record_domain == 'financial_source_document', ValidationIssue.record_id == document_id,
                ValidationIssue.rule == METADATA_POLICY).order_by(ValidationIssue.created_at, ValidationIssue.id)).all()
            reported = [item.evidence.get('metadata') for item in metadata_rows
                        if item.evidence.get('metadata', {}).get('status') == 'reported']
            embedded = next((item.evidence.get('sec_fiscal_metadata') for item in sources
                             if item.evidence.get('sec_fiscal_metadata')), None)
            metadata = reported[-1] if reported else embedded or (
                metadata_rows[-1].evidence.get('metadata') if metadata_rows else None)
            source_decisions = _latest_decisions(session, 'source_financial_fact', [source.id for source in sources])
            existing = session.scalars(select(FundamentalFact).where(
                FundamentalFact.issuer_id == filing.issuer_id,
                FundamentalFact.source_fact_id.in_([source.id for source in sources]),
                FundamentalFact.mapping_version == COMPLETION_POLICY)).all()
            signatures = {(row.source_fact_id, _semantic_signature(row.basis.get('source_fact_signature'),
                row.period, row.basis, document.content_hash, mapping_hash)) for row in existing}
            if len(sources) > max_source_facts:
                local_reasons['completion_source_fact_limit'] += 1
            elif (not filing or not document.extraction_complete or document.disclosure_profile != 'periodic_report'):
                local_reasons['source_extraction_or_disclosure_ineligible'] += 1
            else:
                for source in sources:
                    report.scanned += 1
                    source_decision = source_decisions.get(source.id)
                    if source.issuer_id != filing.issuer_id or not source_decision or source_decision.status != 'accepted':
                        local_reasons['source_adoption_ineligible'] += 1
                        continue
                    adapter = _source_adapter(source)
                    qualification = qualify_source_fact(adapter, document=document,
                        issuer_id=source.issuer_id, metadata=metadata)
                    normalized = map_source_fact(adapter, catalog=catalog,
                        accounting_basis=qualification.basis['accounting_basis'],
                        available_at=max(source.available_at, document.available_at, filing.available_at),
                        captured_at=max(source.captured_at, document.captured_at, filing.captured_at))
                    if normalized.fact is None:
                        local_reasons[normalized.reason or normalized.status] += 1
                        continue
                    report.mapped += 1
                    local_reasons.update(qualification.reasons)
                    if (qualification.basis['fiscal_lineage'] is None
                            and qualification.basis['accounting_basis'] == document.accounting_basis):
                        local_reasons['no_additional_semantic_evidence'] += 1
                        continue
                    if qualification.period['kind'] in {'quarter', 'annual', 'ytd'}:
                        report.fiscal_qualified += 1
                    signature = _semantic_signature(source.signature, qualification.period, qualification.basis,
                        document.content_hash, mapping_hash)
                    if (source.id, signature) in signatures:
                        report.reused += 1
                        continue
                    completed_at = utcnow()
                    clocks = [source.available_at, source.captured_at, source.extracted_at, source.first_seen_at,
                        document.available_at, document.captured_at, filing.available_at, filing.captured_at,
                        source_decision.recorded_at]
                    clocks.extend(row.created_at for row in metadata_rows)
                    if completed_at < max(clocks):
                        local_reasons['input_newer_than_completion_clock'] += 1
                        continue
                    fact = normalized.fact
                    row = FundamentalFact(issuer_id=source.issuer_id, source_fact_id=source.id,
                        metric_id=fact.metric, mapping_version=COMPLETION_POLICY, value=fact.value, unit=fact.unit,
                        period=json_value(qualification.period), basis=json_value({**fact.evidence,
                            **qualification.basis, 'mapping_hash': mapping_hash, 'currency': fact.currency,
                            'dimensions': source.dimensions, 'sign_policy': fact.sign_policy,
                            'source_fact_signature': source.signature, 'source_document_id': document.id,
                            'source_document_hash': document.content_hash, 'source_parser_version': document.parser_version,
                            'source_decision_id': source_decision.id, 'disclosure_profile': document.disclosure_profile,
                            'qualification_reasons': qualification.reasons, 'completion_signature': signature,
                            'qualification_assertion_ids': [item.id for item in metadata_rows],
                            'completion_policy': COMPLETION_POLICY}),
                        available_at=fact.available_at, captured_at=fact.captured_at, materialized_at=completed_at)
                    session.add(row)
                    session.flush()
                    session.add(SelectionDecision(record_domain='fundamental_fact', record_id=row.id,
                        status='accepted', rule_version=COMPLETION_POLICY, evidence_grade='research_only',
                        reason='Exact standard mapping with hash-bound fiscal evidence; missing qualifications remain explicit',
                        actor='financial_completion', recorded_at=completed_at))
                    signatures.add((source.id, signature))
                    report.created += 1
            from .public_availability import resolve_document_availability
            from .publication_models import FinancialPublicationEvidence
            publication = session.scalars(select(FinancialPublicationEvidence).where(
                FinancialPublicationEvidence.document_id == document_id)).all()
            resolution = resolve_document_availability(document, filing, publication,
                history_mode='reconstructed_public', as_of=utcnow()) if filing else None
            public_qualified = bool(resolution and resolution['status'] == 'qualified')
            if not public_qualified:
                local_reasons['historical_public_availability_unqualified'] += 1
            audit, inserted = _annotation(session, document_id=document_id, rule=COMPLETION_POLICY,
                evidence={'audit_schema': AUDIT_SCHEMA, 'document_hash': document.content_hash,
                    'mapped_source_facts': len({identity for identity, signature in signatures}),
                    'canonical_versions': len(signatures),
                    'declared_numeric_nodes': (document.inventory or {}).get('counts', {}).get('numeric_nodes'),
                    'source_budget_deferred': bool(local_reasons.get('completion_source_fact_limit')),
                    'max_source_facts': max_source_facts,
                    'metric_qualification_gaps': _metric_gap_catalog(local_reasons),
                    'qualification_assertion_ids': [item.id for item in metadata_rows],
                    'historical_public_availability_qualified': public_qualified,
                    'public_availability': resolution, 'reasons': dict(sorted(local_reasons.items()))},
                created_at=utcnow(), severity='warning' if local_reasons else 'info')
            report.audited += int(inserted)
            reasons.update(local_reasons)
        session.flush()
    report.reasons = dict(sorted(reasons.items()))
    return report
