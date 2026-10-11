from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import importlib.util

import pytest

from backtest_data.fundamentals.domain import SourceFact


def catalog_module():
    assert importlib.util.find_spec('backtest_data.fundamentals.mapping_catalog'), 'versioned SQL mapping catalog missing'
    from backtest_data.fundamentals import mapping_catalog
    return mapping_catalog


def source(**changes):
    fields = dict(id='source', concept='gaap:RevenueFromContractWithCustomerExcludingAssessedTax',
                  taxonomy='http://fasb.org/us-gaap/2024', context_id='c', value=Decimal('1.23'),
                  raw_value='1.23', unit='USD', original_unit='iso4217:USD', period_kind='duration',
                  period_start=date(2023, 1, 1), period_end=date(2023, 3, 31), scope='unknown')
    fields.update(changes)
    return SourceFact(**fields)


def test_catalog_maps_standard_name_without_claiming_unknown_qualifications():
    module = catalog_module()
    result = module.map_source_fact(source(), accounting_basis='unknown')
    assert result.status == 'mapped'
    assert result.fact.metric == 'revenue'
    assert result.fact.scope == 'unknown'
    assert result.fact.period_kind == 'duration'
    assert result.fact.accounting_basis == 'unknown'
    assert result.fact.fiscal_lineage is None and result.fact.quarter_index is None
    assert result.fact.evidence['mapping_hash'] == module.default_catalog().content_hash


def test_official_https_ifrs_namespace_maps_without_redefining_deployed_v1():
    module=catalog_module()
    fact=source(concept='ifrs-full:Revenue',taxonomy='https://xbrl.ifrs.org/taxonomy/2024-03-27/ifrs-full')
    assert module.map_source_fact(fact).status=='mapped'
    legacy=module.default_catalog('fundamental_mapping_v1')
    assert legacy.content_hash=='3f4a3554b31e6e51d19ad9811bb6db0e6d289172a146a538c658ac062297f89f'
    assert module.default_catalog().version=='fundamental_mapping_v2'
    assert module.map_source_fact(fact,catalog=legacy).status=='unmapped'


@pytest.mark.parametrize('changes', [
    {'taxonomy': 'http://fasb.org.us-gaap.attacker/2024'},
    {'taxonomy': 'http://fasb.org/us-gaap/2024/custom'},
    {'taxonomy': 'urn:company:us-gaap'},
    {'dimensions': {'SegmentAxis': 'East'}},
    {'unit': 'shares'},
    {'period_kind': 'instant'},
    {'concept': 'custom:UsefulNote'},
])
def test_catalog_rejects_unsupported_taxonomy_dimensions_units_and_period(changes):
    assert catalog_module().map_source_fact(source(**changes), accounting_basis='unknown').fact is None


def test_catalog_nil_and_per_share_units_are_not_silently_reinterpreted():
    module = catalog_module()
    assert module.map_source_fact(source(is_nil=True, value=None), accounting_basis='unknown').status == 'unavailable'
    eps = source(concept='u:EarningsPerShareDiluted', unit='USD/shares', scope='unknown')
    mapped = module.map_source_fact(eps, accounting_basis='unknown')
    assert mapped.fact.metric == 'eps_diluted' and mapped.fact.unit == 'USD/share'
    assert mapped.fact.scope == 'unknown' and mapped.fact.share_basis is None
    assert module.map_source_fact(replace(eps, unit='USD'), accounting_basis='unknown').fact is None


def test_catalog_hash_changes_with_rule_semantics():
    module = catalog_module()
    original = module.default_catalog()
    changed = replace(original, entries=(replace(original.entries[0], metric='different'),) + original.entries[1:])
    assert changed.content_hash != original.content_hash
    assert len(original.content_hash) == 64


def test_qualification_adapter_requires_explicit_metadata_and_does_not_guess_quarters():
    assert importlib.util.find_spec('backtest_data.fundamentals.materialize'), 'SQL materialization adapter missing'
    from backtest_data.fundamentals.materialize import canonical_to_financial_fact
    from backtest_data.fundamentals.models import FundamentalFact
    now = datetime(2024, 5, 1, tzinfo=timezone.utc)
    row = FundamentalFact(id='canonical', source_fact_id='source', issuer_id='issuer', metric_id='revenue',
        mapping_version='fundamental_mapping_v1', value=Decimal(1), unit='USD',
        period={'kind': 'duration', 'start': '2023-01-01', 'end': '2023-03-31'},
        basis={'scope': 'unknown', 'currency': 'USD', 'accounting_basis': 'unknown'},
        available_at=now, captured_at=now, materialized_at=now)
    fact = canonical_to_financial_fact(row)
    assert fact.period_kind == 'duration' and fact.quarter_index is None
    assert fact.fiscal_lineage is None and fact.scope == 'unknown'
    row.period = {**row.period, 'kind': 'quarter', 'fiscal_year': 2023, 'fiscal_quarter': 1, 'quarter_index': 8092}
    with pytest.raises(ValueError, match='explicit_fiscal_qualification_required'):
        canonical_to_financial_fact(row)


def test_catalog_retains_depletion_and_tax_inclusive_revenue_as_unmapped():
    module = catalog_module()
    for concept in ('DepreciationDepletionAndAmortization', 'RevenueFromContractWithCustomerIncludingAssessedTax'):
        assert module.map_source_fact(source(concept='g:' + concept), accounting_basis='unknown').fact is None


def test_catalog_validates_ifrs_namespace_date_and_prefix_binding():
    module = catalog_module()
    valid = source(concept='i:Revenue', taxonomy='http://xbrl.ifrs.org/taxonomy/2024-03-27/ifrs-full')
    assert module.map_source_fact(valid, accounting_basis='unknown').fact.metric == 'revenue'
    assert module.map_source_fact(replace(valid, taxonomy='http://xbrl.ifrs.org/taxonomy/2024-99-99/ifrs-full'), accounting_basis='unknown').fact is None
    rebound = replace(valid, evidence={'namespace_bindings': {'i': 'urn:custom'}})
    assert module.map_source_fact(rebound, accounting_basis='unknown').fact is None


def test_mapping_refuses_invalid_duration_order_or_nonfinite_source_values():
    module = catalog_module()
    assert module.map_source_fact(source(period_start=date(2024, 1, 1)), accounting_basis='unknown').fact is None
    assert module.map_source_fact(source(value=Decimal('NaN')), accounting_basis='unknown').fact is None


@pytest.mark.parametrize('changes', [
    {'directly_reported': 'false'}, {'period_compatible': 'true'}, {'revision': '99'},
    {'component_ids': 'pretend_component'}, {'qualification_evidence': 'unstructured_claim'},
    {'accounting_basis': 'invented_framework'},
])
def test_qualification_adapter_refuses_malformed_or_unregistered_basis(changes):
    from backtest_data.fundamentals.materialize import canonical_to_financial_fact
    from backtest_data.fundamentals.models import FundamentalFact
    now = datetime(2024, 5, 1, tzinfo=timezone.utc)
    row = FundamentalFact(id='c', issuer_id='issuer', source_fact_id='s', metric_id='revenue', mapping_version='test',
        value=Decimal(100), unit='USD', available_at=now, captured_at=now, materialized_at=now,
        period={'kind': 'quarter', 'start': '2023-01-01', 'end': '2023-03-31', 'fiscal_year': 2023,
                'fiscal_quarter': 1, 'quarter_index': 8092, 'fiscal_cycle': '2023'},
        basis={'scope': 'consolidated', 'currency': 'USD', 'accounting_basis': 'US_GAAP',
               'fiscal_lineage': 'reported-cycle', 'qualification_evidence': ['document:explicit-fiscal-calendar'], **changes})
    with pytest.raises(ValueError, match='invalid_canonical|explicit_fiscal'):
        canonical_to_financial_fact(row)


def test_unknown_share_basis_stays_missing_for_per_share_arithmetic():
    from backtest_data.fundamentals.materialize import canonical_to_financial_fact
    from backtest_data.fundamentals.models import FundamentalFact
    now = datetime(2024, 5, 1, tzinfo=timezone.utc)
    row = FundamentalFact(id='c', issuer_id='issuer', source_fact_id='s', metric_id='eps_diluted', mapping_version='test',
        value=Decimal(1), unit='USD/share', available_at=now, captured_at=now, materialized_at=now,
        period={'kind': 'duration', 'start': '2023-01-01', 'end': '2023-03-31'},
        basis={'scope': 'unknown', 'currency': 'USD', 'accounting_basis': 'unknown', 'share_basis': 'unknown'})
    assert canonical_to_financial_fact(row).share_basis is None
