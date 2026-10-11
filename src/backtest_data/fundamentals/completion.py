"""Source-bound financial context completion; publication clocks stay independent.

DEI describes a report focus. It does not by itself describe every numeric
duration, a consolidated statement, or a comparable common share basis.
"""
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
import hashlib
from io import BytesIO
import json
import re
from xml.etree import ElementTree as ET

from .derive import quarter_from_ytd
from .domain import FinancialFact, SourceFact, aware, stable_hash
from .mapping_catalog import default_catalog, local_concept, taxonomy_group
from .metric_policy import EPS_METRICS
from .selection import fact_visible
from .xbrl import (IX_NAMESPACES, MAX_EMITTED_BYTES, UnsafeFinancialDocument,
                   _NamespaceScope, _content, _split, parse_xbrl)


METADATA_POLICY = 'sec_fiscal_metadata_v1'
COMPLETION_POLICY = 'financial_semantic_completion_v1'
DEI_CONCEPTS = {'DocumentType', 'DocumentFiscalYearFocus', 'DocumentFiscalPeriodFocus',
    'DocumentPeriodEndDate', 'CurrentFiscalYearEndDate', 'DocumentTransitionReport'}
DEI_NAMESPACE = re.compile(r'https?://(?:xbrl\.sec\.gov|xbrl\.us)/dei/20(?:0[9]|1[0-9]|2[0-6])(?:-\d{2}-\d{2})?')
FORMS = {'10-K', '10-K/A', '10-Q', '10-Q/A', '20-F', '20-F/A', '40-F', '40-F/A'}


def _sec_metadata(content, parsed, form):
    """Called only after the bounded numeric parser has validated XML safety."""
    stack, pending, bindings, rows = [], {}, {}, []
    for event, node in ET.iterparse(BytesIO(content), events=('start-ns', 'start', 'end')):
        if event == 'start-ns':
            pending[node[0]] = node[1]
        elif event == 'start':
            active = (_NamespaceScope(pending, stack[-1] if stack else None)
                      if pending or not stack else stack[-1])
            stack.append(active)
            bindings[id(node)] = active
            pending = {}
        else:
            stack.pop()
    root = node
    for node in root.iter():
        namespace, name = _split(node.tag)
        if namespace in IX_NAMESPACES and name == 'nonNumeric':
            lexical = node.get('name', '')
            prefix, separator, name = lexical.partition(':')
            namespace = bindings[id(node)].get(prefix, '') if separator else bindings[id(node)].get('', '')
        if name not in DEI_CONCEPTS or not DEI_NAMESPACE.fullmatch(namespace):
            continue
        unsupported = any(key in node.attrib for key in ('continuedAt', 'format'))
        nil = node.get('{http://www.w3.org/2001/XMLSchema-instance}nil') in {'true', '1'}
        rows.append({'concept': f'{{{namespace}}}{name}', 'local_name': name,
            'context_id': node.get('contextRef', ''), 'value': _content(node, max_chars=128).strip(),
            'status': 'unsupported_metadata_transform_or_continuation' if unsupported else ('nil' if nil else 'ok')})
        if len(rows) > 128:
            return {'policy': METADATA_POLICY, 'document_hash': hashlib.sha256(content).hexdigest(),
                    'form': form, 'status': 'unavailable', 'reason': 'fiscal_metadata_node_limit'}
    evidence = {'policy': METADATA_POLICY, 'document_hash': hashlib.sha256(content).hexdigest(),
        'form': form, 'status': 'unavailable', 'rows': rows}
    needed = DEI_CONCEPTS - {'DocumentTransitionReport'}
    values = {}
    for name in needed:
        facts = [row for row in rows if row['local_name'] == name]
        unique = {(row['context_id'], row['value'], row['status']) for row in facts}
        if not facts or len(unique) != 1 or facts[0]['status'] != 'ok':
            return {**evidence, 'reason': 'missing_or_conflicting_fiscal_metadata'}
        values[name] = facts[0]
    context_ids = {row['context_id'] for row in values.values()}
    if len(context_ids) != 1:
        return {**evidence, 'reason': 'fiscal_metadata_context_mismatch'}
    context = parsed.contexts.get(next(iter(context_ids)))
    try:
        year = int(values['DocumentFiscalYearFocus']['value'])
        end = date.fromisoformat(values['DocumentPeriodEndDate']['value'])
        fye = values['CurrentFiscalYearEndDate']['value']
        if not re.fullmatch(r'--\d{2}-\d{2}', fye):
            raise ValueError()
        date.fromisoformat('2000' + fye[1:])
    except (ValueError, TypeError):
        return {**evidence, 'reason': 'invalid_fiscal_metadata_value'}
    focus = values['DocumentFiscalPeriodFocus']['value']
    document_type = values['DocumentType']['value']
    transitions = [row for row in rows if row['local_name'] == 'DocumentTransitionReport']
    if any(row['status'] != 'ok' or row['value'] not in {'false', '0'} for row in transitions):
        return {**evidence, 'reason': 'transition_or_unqualified_fiscal_period'}
    if (form not in FORMS or document_type != form or not 1900 <= year <= 9999
            or focus not in {'Q1', 'Q2', 'Q3', 'FY'}
            or (form.startswith('10-Q') and focus == 'FY')
            or (not form.startswith('10-Q') and focus != 'FY')):
        return {**evidence, 'reason': 'unsupported_fiscal_report_focus'}
    if (context is None or context.dimensions or context.period_kind != 'duration'
            or context.period_start is None or context.period_end != end
            or not context.entity_identifier or not context.entity_scheme):
        return {**evidence, 'reason': 'unqualified_fiscal_metadata_context'}
    return {**evidence, 'status': 'reported', 'context_id': context.id,
        'entity_identifier': context.entity_identifier, 'entity_scheme': context.entity_scheme,
        'period_start': context.period_start.isoformat(), 'period_end': end.isoformat(),
        'fiscal_year': year, 'focus': focus, 'fiscal_year_end': fye}


def parse_sec_xbrl(content: bytes, *, form: str, **parser_limits):
    """Preserve six bounded DEI fields alongside unchanged numeric inventories."""
    parsed = parse_xbrl(content, **parser_limits)
    try:
        metadata = _sec_metadata(content, parsed, form)
    except UnsafeFinancialDocument:
        metadata = {'policy': METADATA_POLICY, 'document_hash': hashlib.sha256(content).hexdigest(),
            'form': form, 'status': 'unavailable', 'reason': 'fiscal_metadata_lexical_limit'}
    metadata_bytes = len(json.dumps(metadata, ensure_ascii=False, separators=(',', ':')).encode())
    limit = parser_limits.get('max_emitted_bytes', MAX_EMITTED_BYTES)
    if parsed.inventory.emitted_bytes + metadata_bytes > limit:
        metadata = {'policy': METADATA_POLICY, 'document_hash': hashlib.sha256(content).hexdigest(),
            'form': form, 'status': 'unavailable', 'reason': 'fiscal_metadata_emitted_evidence_limit'}
        metadata_bytes = len(json.dumps(metadata, separators=(',', ':')).encode())
    emitted = parsed.inventory.emitted_bytes + (metadata_bytes if parsed.facts else 0)
    if emitted > limit:
        # The numeric parser's own budget passed. Even a tiny unavailable marker
        # must not discard its validated numeric inventory.
        return replace(parsed, parser_version='financial_extraction_v5_sec_metadata_budget_unavailable_v1')
    facts = parsed.facts
    if facts:
        facts = (replace(facts[0], evidence={**facts[0].evidence, 'sec_fiscal_metadata': metadata}), *facts[1:])
    return replace(parsed, facts=facts,
        inventory=replace(parsed.inventory, emitted_bytes=emitted),
        parser_version='financial_extraction_v5_sec_context_v1')


@dataclass(frozen=True)
class Qualification:
    period: dict
    basis: dict
    reasons: tuple[str, ...]


def qualify_source_fact(source: SourceFact, *, document, issuer_id, metadata=None) -> Qualification:
    """Qualify only exact reported Q1/FY context; preserve all unknown semantics."""
    period = {'kind': source.period_kind, 'start': source.period_start, 'end': source.period_end}
    accounting = document.accounting_basis
    group = taxonomy_group(source.taxonomy, default_catalog())
    if accounting in {None, '', 'unknown'} and group in {'us-gaap', 'ifrs-full'}:
        accounting = {'us-gaap': 'US_GAAP', 'ifrs-full': 'IFRS'}[group]
    basis = {'scope': source.scope, 'accounting_basis': accounting, 'share_basis': None,
             'fiscal_lineage': None, 'qualification_evidence': [], 'directly_reported': True}
    reasons = []
    metadata = metadata or source.evidence.get('sec_fiscal_metadata')
    if not isinstance(metadata, dict):
        reasons.append('fiscal_metadata_emitted_evidence_limit' if getattr(document, 'parser_version', '')
            == 'financial_extraction_v5_sec_metadata_budget_unavailable_v1' else 'missing_fiscal_metadata')
    elif metadata.get('document_hash') != document.content_hash or metadata.get('policy') != METADATA_POLICY:
        reasons.append('fiscal_metadata_document_hash_mismatch')
    elif metadata.get('status') != 'reported':
        reasons.append(metadata.get('reason', 'unqualified_fiscal_metadata'))
    elif (document.inventory or {}).get('issuer_identity', {}).get('status') != 'matched':
        reasons.append('issuer_identity_unqualified')
    else:
        context = (document.inventory or {}).get('contexts', {}).get(source.context_id, {})
        context_entity = (context.get('entity_identifier'), context.get('entity_scheme'))
        expected_entity = (metadata.get('entity_identifier'), metadata.get('entity_scheme'))
        report_start = date.fromisoformat(metadata['period_start'])
        report_end = date.fromisoformat(metadata['period_end'])
        balance_boundary = source.period_kind == 'instant' and source.period_end in {
            report_end, report_start - timedelta(days=1)}
        exact_period = balance_boundary or (source.period_start and source.period_start.isoformat() == metadata['period_start']
                        and source.period_end and source.period_end.isoformat() == metadata['period_end'])
        if context_entity != expected_entity or source.dimensions or not exact_period:
            reasons.append('source_period_outside_reported_fiscal_context')
        elif metadata['focus'] not in {'Q1', 'FY'}:
            reasons.append('fiscal_year_start_evidence_required')
        else:
            days = (report_end - report_start).days + 1
            low, high = (60, 120) if metadata['focus'] == 'Q1' else (330, 385)
            if not low <= days <= high:
                reasons.append('nonstandard_fiscal_duration')
            else:
                year = metadata['fiscal_year']
                quarter = 1 if metadata['focus'] == 'Q1' else 4
                if not balance_boundary:
                    period.update(kind='quarter' if quarter == 1 else 'annual', fiscal_year=year,
                        fiscal_quarter=quarter, quarter_index=year * 4 + quarter - 1,
                        fiscal_cycle=f'{issuer_id}:{year}:{source.period_start.isoformat()}')
                basis.update(fiscal_lineage=f"sec:{issuer_id}:{metadata['fiscal_year_end']}",
                    qualification_evidence=[f"{METADATA_POLICY}:{document.content_hash}:{metadata['context_id']}"],
                    sec_fiscal_metadata=metadata)
    if source.scope == 'unknown':
        reasons.append('unknown_scope')
    if accounting in {None, '', 'unknown'}:
        reasons.append('missing_accounting_basis')
    if local_concept(source) in {'EarningsPerShareBasic', 'EarningsPerShareDiluted',
                                 'BasicEarningsLossPerShare', 'DilutedEarningsLossPerShare'}:
        reasons.append('missing_share_basis')
    return Qualification(period, basis, tuple(dict.fromkeys(reasons)))


def original_metadata_qualified(basis, evidence, resolution) -> bool:
    """Require the selected original body to cover nonnumeric fiscal metadata.

    A numeric equivalence certificate can prove original numeric facts while
    failing to prove captured DEI labels. This additional gate is deliberately
    independent of the resolver's numeric/public-clock certificate validation.
    No unregistered future metadata-digest field substitutes for exact bytes.
    """
    if not isinstance(basis, dict):
        return False
    if not basis.get('sec_fiscal_metadata') and not basis.get('qualification_assertion_ids'):
        return True
    if not isinstance(resolution, dict) or resolution.get('status') != 'qualified':
        return False
    digest = basis.get('source_document_hash')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        return False
    selected_ids = set(resolution.get('evidence_ids') or ())
    def field(row, name):
        return row.get(name) if isinstance(row, dict) else getattr(row, name, None)
    selected = [row for row in evidence if field(row, 'id') in selected_ids]
    if not selected or {field(row, 'id') for row in selected} != selected_ids:
        return False
    return all(field(row, 'status') == 'qualified' and field(row, 'document_hash') == digest
        and isinstance(field(row, 'evidence'), dict)
        and field(row, 'evidence').get('proof', {}).get('original_body_hash') == digest for row in selected)


def complete_quarter_inputs(facts, *, as_of: datetime, history_mode='reconstructed_public', materialized_at=None):
    """Add non-EPS quarters from already-qualified, visible adjacent YTD inputs.

    Call after publication revision selection. Reported quarters take precedence;
    ambiguous cumulative inputs never synthesize a quarter. Original canonical
    IDs remain the complete arithmetic dependency closure.
    """
    facts = tuple(fact for fact in facts if fact_visible(fact, as_of=as_of, history_mode=history_mode))
    completed = list(facts)
    def series(fact):
        return (fact.metric, fact.fiscal_lineage, fact.fiscal_cycle, fact.scope, fact.currency,
                fact.accounting_basis, fact.unit, fact.dimensions, fact.share_basis)
    reported = {(series(fact), fact.quarter_index) for fact in facts if fact.period_kind == 'quarter'}
    for current in facts:
        if (current.metric in EPS_METRICS or current.period_kind not in {'ytd', 'annual'}
                or current.fiscal_quarter not in {2, 3, 4} or current.quarter_index is None):
            continue
        if (series(current), current.quarter_index) in reported:
            continue
        same_current = [fact for fact in facts if series(fact) == series(current)
                        and fact.period_kind in {'ytd', 'annual'} and fact.quarter_index == current.quarter_index]
        previous = [fact for fact in facts if series(fact) == series(current)
            and fact.period_kind in {'ytd', 'quarter'} and fact.fiscal_quarter == current.fiscal_quarter - 1
            and fact.quarter_index == current.quarter_index - 1
            and fact.period_start == current.period_start]
        if len(previous) != 1 or len(same_current) != 1:
            continue
        calculated = quarter_from_ytd(current, previous[0], as_of=as_of,
            history_mode=history_mode, materialized_at=materialized_at)
        if calculated.reason:
            continue
        clocks = {key: max(getattr(fact, key) for fact in (current, previous[0]))
            if all(getattr(fact, key) is not None for fact in (current, previous[0])) else None
            for key in ('captured_at', 'first_seen_at', 'extracted_at', 'decision_recorded_at')}
        completed.append(replace(current, id='quarter:' + stable_hash(calculated.input_record_ids),
            period_kind='quarter', period_start=previous[0].period_end + timedelta(days=1),
            value=calculated.value, directly_reported=False, available_at=calculated.available_at,
            materialized_at=materialized_at, source_record_ids=calculated.input_record_ids, **clocks,
            evidence={**current.evidence, **calculated.evidence, 'completion_policy': COMPLETION_POLICY}))
    return tuple(completed)
