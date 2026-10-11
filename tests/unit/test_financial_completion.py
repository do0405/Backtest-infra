from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import importlib
from types import SimpleNamespace

import pytest


NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def module():
    try:
        return importlib.import_module('backtest_data.fundamentals.completion')
    except ModuleNotFoundError:
        pytest.fail('financial completion module is missing')


def instance(focus='Q1', *, start='2023-01-01', end='2023-03-31', extra=''):
    return f'''<xbrl xmlns="http://www.xbrl.org/2003/instance"
      xmlns:d="http://xbrl.sec.gov/dei/2024" xmlns:g="http://fasb.org/us-gaap/2024"
      xmlns:u="http://www.xbrl.org/2003/iso4217">
      <context id="focus"><entity><identifier scheme="http://www.sec.gov/CIK">1</identifier></entity>
        <period><startDate>{start}</startDate><endDate>{end}</endDate></period></context>
      <unit id="USD"><measure>u:USD</measure></unit>
      <d:DocumentType contextRef="focus">{'10-K' if focus == 'FY' else '10-Q'}</d:DocumentType>
      <d:DocumentFiscalYearFocus contextRef="focus">2023</d:DocumentFiscalYearFocus>
      <d:DocumentFiscalPeriodFocus contextRef="focus">{focus}</d:DocumentFiscalPeriodFocus>
      <d:DocumentPeriodEndDate contextRef="focus">{end}</d:DocumentPeriodEndDate>
      <d:CurrentFiscalYearEndDate contextRef="focus">--12-31</d:CurrentFiscalYearEndDate>
      <g:Revenues contextRef="focus" unitRef="USD" decimals="0">100</g:Revenues>{extra}
    </xbrl>'''.encode()


def source_and_document(parsed, content):
    source = replace(parsed.facts[0], id='source')
    document = SimpleNamespace(id='document', content_hash=hashlib.sha256(content).hexdigest(),
        accounting_basis='unknown', inventory={'issuer_identity': {'status': 'matched'}, 'contexts': {
            key: {'entity_identifier': ctx.entity_identifier, 'entity_scheme': ctx.entity_scheme,
                  'period_kind': ctx.period_kind, 'period_start': ctx.period_start.isoformat() if ctx.period_start else None,
                  'period_end': ctx.period_end.isoformat(), 'dimensions': ctx.dimensions}
            for key, ctx in parsed.contexts.items()}})
    return source, document


def test_sec_dei_metadata_qualifies_reported_q1_without_assuming_scope():
    m = module()
    content = instance()
    parsed = m.parse_sec_xbrl(content, form='10-Q')
    source, document = source_and_document(parsed, content)
    qualified = m.qualify_source_fact(source, document=document, issuer_id='issuer')
    assert qualified.period == {'kind': 'quarter', 'start': date(2023, 1, 1),
        'end': date(2023, 3, 31), 'fiscal_year': 2023, 'fiscal_quarter': 1,
        'quarter_index': 2023 * 4, 'fiscal_cycle': 'issuer:2023:2023-01-01'}
    assert qualified.basis['accounting_basis'] == 'US_GAAP'
    assert qualified.basis['scope'] == 'unknown'
    assert qualified.basis['share_basis'] is None
    assert qualified.reasons == ('unknown_scope',)
    assert qualified.basis['fiscal_lineage'] == 'sec:issuer:--12-31'
    assert parsed.inventory.numeric_nodes == parsed.inventory.extracted_nodes == 1


def test_sec_dei_q2_retains_report_focus_without_inventing_fiscal_year_start():
    m = module()
    content = instance('Q2', end='2023-06-30')
    parsed = m.parse_sec_xbrl(content, form='10-Q')
    source, document = source_and_document(parsed, content)
    qualified = m.qualify_source_fact(source, document=document, issuer_id='issuer')
    assert qualified.period['kind'] == 'duration'
    assert 'fiscal_year_start_evidence_required' in qualified.reasons
    standalone = replace(source, context_id='quarter', period_start=date(2023, 4, 1))
    unqualified = m.qualify_source_fact(standalone, document=document, issuer_id='issuer')
    assert unqualified.period['kind'] == 'duration'
    assert 'source_period_outside_reported_fiscal_context' in unqualified.reasons


def test_missing_dei_and_conflicting_focus_stay_unqualified():
    m = module()
    for content in (instance().replace(b'<d:DocumentFiscalPeriodFocus contextRef="focus">Q1</d:DocumentFiscalPeriodFocus>', b''),
        instance(extra='<d:DocumentFiscalYearFocus contextRef="focus">2024</d:DocumentFiscalYearFocus>')):
        parsed = m.parse_sec_xbrl(content, form='10-Q')
        source, document = source_and_document(parsed, content)
        result = m.qualify_source_fact(source, document=document, issuer_id='issuer')
        assert result.period['kind'] == 'duration'
        assert result.basis['fiscal_lineage'] is None
        assert any('fiscal_metadata' in reason for reason in result.reasons)


def test_fiscal_metadata_is_bound_to_exact_document_and_rejects_short_annual_cycle():
    m = module()
    content = instance('FY', start='2023-07-01', end='2023-12-31')
    parsed = m.parse_sec_xbrl(content, form='10-K')
    source, document = source_and_document(parsed, content)
    result = m.qualify_source_fact(source, document=document, issuer_id='issuer')
    assert 'nonstandard_fiscal_duration' in result.reasons
    assert result.period['kind'] == 'duration'
    document.content_hash = 'f' * 64
    changed = m.qualify_source_fact(source, document=document, issuer_id='issuer')
    assert 'fiscal_metadata_document_hash_mismatch' in changed.reasons


def financial(metric, quarter, kind, value, *, start=date(2023, 1, 1)):
    from backtest_data.fundamentals.domain import FinancialFact
    ends = (date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31))
    return FinancialFact(id=f'{metric}{quarter}', metric=metric, value=Decimal(value),
        period_kind=kind, period_start=start, period_end=ends[quarter-1], unit='USD',
        scope='consolidated', available_at=NOW, captured_at=NOW,
        fiscal_year=2023, fiscal_quarter=quarter, quarter_index=2023*4+quarter-1,
        fiscal_cycle='issuer:2023:2023-01-01', fiscal_lineage='sec:issuer:--12-31',
        currency='USD', accounting_basis='US_GAAP', source_record_ids=(f'{metric}{quarter}',))


def test_quarter_completion_uses_qualified_ytd_arithmetic_with_full_input_closure():
    m = module()
    facts = (financial('revenue', 1, 'quarter', '100'), financial('revenue', 2, 'ytd', '230'),
        financial('revenue', 3, 'ytd', '390'), financial('revenue', 4, 'annual', '600'))
    completed = m.complete_quarter_inputs(facts, as_of=NOW, materialized_at=NOW)
    quarters = sorted((fact for fact in completed if fact.period_kind == 'quarter'), key=lambda f: f.quarter_index)
    assert [fact.value for fact in quarters] == list(map(Decimal, ('100', '130', '160', '210')))
    assert quarters[-1].period_start == date(2023, 10, 1)
    assert quarters[-1].input_ids == ('revenue4', 'revenue3')
    assert quarters[-1].directly_reported is False
    from backtest_data.fundamentals.ratios import calculate_metric
    ttm = calculate_metric('revenue_ttm', completed, as_of=NOW, materialized_at=NOW)
    assert ttm.value == Decimal(600)
    assert set(ttm.input_record_ids) == {'revenue1', 'revenue2', 'revenue3', 'revenue4'}


def test_quarter_completion_preserves_reported_quarters_and_never_differences_eps():
    m = module()
    first = financial('revenue', 1, 'quarter', '100')
    reported = financial('revenue', 2, 'quarter', '135', start=date(2023, 4, 1))
    cumulative = financial('revenue', 2, 'ytd', '230')
    eps = tuple(replace(financial('eps_basic', q, kind, '1'), unit='USD/share',
                        scope='parent_common', share_basis='qualified_common_basis')
                for q, kind in ((1, 'quarter'), (2, 'ytd')))
    result = m.complete_quarter_inputs((first, reported, cumulative, *eps), as_of=NOW, materialized_at=NOW)
    assert [fact for fact in result if fact.metric == 'revenue' and fact.fiscal_quarter == 2 and fact.period_kind == 'quarter'] == [reported]
    assert not [fact for fact in result if fact.metric == 'eps_basic' and fact.fiscal_quarter == 2 and fact.period_kind == 'quarter']


def test_quarter_completion_filters_future_restatements_before_arithmetic():
    m = module()
    first = financial('revenue', 1, 'quarter', '100')
    later = replace(financial('revenue', 2, 'ytd', '230'), available_at=datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert not [fact for fact in m.complete_quarter_inputs((first, later), as_of=NOW, materialized_at=NOW)
                if fact.period_kind == 'quarter' and fact.fiscal_quarter == 2]


def test_sec_metadata_attachment_obeys_the_existing_emitted_evidence_budget():
    m = module()
    from backtest_data.fundamentals.xbrl import parse_xbrl
    content = instance()
    numeric_only = parse_xbrl(content)
    budget = numeric_only.inventory.emitted_bytes + 1000
    parse_xbrl(content, max_emitted_bytes=budget)
    result = m.parse_sec_xbrl(content, form='10-Q', max_emitted_bytes=budget)
    assert result.complete and result.facts[0].value == Decimal(100)
    assert result.inventory.numeric_nodes == result.inventory.extracted_nodes == 1
    assert result.inventory.emitted_bytes <= budget
    assert result.facts[0].evidence['sec_fiscal_metadata']['status'] == 'unavailable'
    assert result.facts[0].evidence['sec_fiscal_metadata']['reason'] == 'fiscal_metadata_emitted_evidence_limit'


def test_instant_balance_uses_explicit_reported_fiscal_boundary_without_a_duration_guess():
    m = module()
    extra = '''<context id="closing"><entity><identifier scheme="http://www.sec.gov/CIK">1</identifier></entity>
        <period><instant>2023-03-31</instant></period></context>
      <g:Assets contextRef="closing" unitRef="USD" decimals="0">900</g:Assets>'''
    content = instance(extra=extra)
    parsed = m.parse_sec_xbrl(content, form='10-Q')
    source, document = source_and_document(parsed, content)
    balance = replace(parsed.facts[1], id='balance')
    qualified = m.qualify_source_fact(balance, document=document, issuer_id='issuer',
        metadata=parsed.facts[0].evidence['sec_fiscal_metadata'])
    assert qualified.period['kind'] == 'instant'
    assert qualified.basis['fiscal_lineage'] == 'sec:issuer:--12-31'
    assert qualified.period.get('fiscal_year') is None
    assert qualified.reasons == ('unknown_scope',)
    assert sum('sec_fiscal_metadata' in fact.evidence for fact in parsed.facts) == 1


def test_unverified_issuer_and_mixed_dei_entity_never_establish_fiscal_lineage():
    m = module()
    content = instance()
    parsed = m.parse_sec_xbrl(content, form='10-Q')
    source, document = source_and_document(parsed, content)
    document.inventory['issuer_identity']['status'] = 'unverified'
    result = m.qualify_source_fact(source, document=document, issuer_id='issuer')
    assert result.basis['fiscal_lineage'] is None
    assert 'issuer_identity_unqualified' in result.reasons
    document.inventory['issuer_identity']['status'] = 'matched'
    document.inventory['contexts']['focus']['entity_identifier'] = '2'
    mixed = m.qualify_source_fact(source, document=document, issuer_id='issuer')
    assert mixed.basis['fiscal_lineage'] is None
    assert 'source_period_outside_reported_fiscal_context' in mixed.reasons


def test_recognized_ifrs_taxonomy_establishes_accounting_vocabulary_without_a_fiscal_guess():
    m = module()
    content = instance()
    parsed = m.parse_sec_xbrl(content, form='10-Q')
    source, document = source_and_document(parsed, content)
    source = replace(source, concept='f:Revenue', taxonomy='https://xbrl.ifrs.org/taxonomy/2024-03-27/ifrs-full',
                     scope='consolidated', evidence={})
    result = m.qualify_source_fact(source, document=document, issuer_id='issuer')
    assert result.basis['accounting_basis'] == 'IFRS'
    assert result.basis['scope'] == 'consolidated' and result.basis['fiscal_lineage'] is None
    assert result.reasons == ('missing_fiscal_metadata',)


def test_original_fiscal_metadata_requires_the_exact_selected_original_body_proof():
    m = module()
    original = 'a' * 64
    future = 'b' * 64
    basis = {'source_document_hash': future, 'sec_fiscal_metadata': {'document_hash': future, 'focus': 'Q1'}}
    numeric_only = SimpleNamespace(id='certificate', status='qualified', document_hash=future,
        evidence={'proof': {'original_body_hash': original, 'version_match': 'semantic_numeric_equivalence'}})
    resolution = {'status': 'qualified', 'evidence_ids': ['certificate']}
    assert m.original_metadata_qualified(basis, [numeric_only], resolution) is False
    exact = SimpleNamespace(id='exact', status='qualified', document_hash=future,
        evidence={'proof': {'original_body_hash': future}})
    assert m.original_metadata_qualified(basis, [exact], {'status': 'qualified', 'evidence_ids': ['exact']})
    assert m.original_metadata_qualified(basis, [exact], resolution) is False
    assert m.original_metadata_qualified(basis, [exact], {'status': 'capture_only', 'evidence_ids': ['exact']}) is False
    assert m.original_metadata_qualified({'scope': 'consolidated'}, [], resolution)


def test_metadata_assertion_dependencies_cannot_be_qualified_by_numeric_only_publication():
    m = module()
    basis = {'source_document_hash': 'b' * 64, 'qualification_assertion_ids': ['metadata-assertion']}
    certificate = SimpleNamespace(id='certificate', status='qualified', document_hash='b' * 64,
        evidence={'proof': {'original_body_hash': 'a' * 64}})
    assert not m.original_metadata_qualified(basis, [certificate], {'status': 'qualified', 'evidence_ids': ['certificate']})


def test_sql_gap_catalog_includes_every_valuation_without_guessing_price_or_share_inputs():
    from backtest_data.fundamentals.completion_storage import _metric_gap_catalog
    from backtest_data.fundamentals.materialize import VALUATION_METRICS
    gaps = _metric_gap_catalog({})
    for metric in VALUATION_METRICS:
        assert metric in gaps
        assert gaps[metric]['status'] == 'document_input_unqualified'
        assert gaps[metric]['reasons'] == ['price_share_class_lineage_unavailable']
