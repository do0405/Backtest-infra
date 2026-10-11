from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest_data.fundamentals.domain import FinancialFact, SourceFact


NOW = datetime(2022, 6, 1, tzinfo=timezone.utc)


def fact(metric='revenue', value='100', *, year=2021, quarter=1, kind='quarter', **changes):
    starts = (date(year, 1, 1), date(year, 4, 1), date(year, 7, 1), date(year, 10, 1))
    ends = (date(year, 3, 31), date(year, 6, 30), date(year, 9, 30), date(year, 12, 31))
    fields = dict(id=f'{metric}:{year}Q{quarter}', metric=metric, value=Decimal(value) if value is not None else None,
                  period_kind=kind, period_start=starts[quarter-1] if kind == 'quarter' else starts[0],
                  period_end=ends[quarter-1], unit='USD/share' if metric.startswith('eps_') else 'USD',
                  scope='parent_common' if metric.startswith('eps_') else 'consolidated',
                  available_at=NOW-timedelta(days=100), captured_at=NOW-timedelta(days=90),
                  first_seen_at=NOW-timedelta(days=90), extracted_at=NOW-timedelta(days=90),
                  decision_recorded_at=NOW-timedelta(days=80), fiscal_year=year, fiscal_quarter=quarter,
                  quarter_index=year*4+quarter-1, fiscal_cycle=str(year), fiscal_lineage='calendar',
                  currency='USD', share_basis='split:2022' if metric.startswith('eps_') else None,
                  accounting_basis='US_GAAP')
    fields.update(changes)
    return FinancialFact(**fields)


def test_mapping_requires_exact_taxonomy_scope_and_dimensions_and_keeps_unmapped():
    from backtest_data.fundamentals.normalize import MappingRule, normalize_fact
    source = SourceFact('src', 'custom:RevenueNote', 'urn:custom', 'C', Decimal('100'), '100', 'USD', 'iso4217:USD',
                        period_kind='duration', period_start=date(2021, 1, 1), period_end=date(2021, 3, 31),
                        scope='consolidated', dimensions={'custom:Region': 'custom:North'})
    rule = MappingRule('urn:custom', 'custom:RevenueNote', 'revenue', 'consolidated',
                       dimensions={'custom:Region': 'custom:North'})
    metadata = dict(available_at=NOW, captured_at=NOW, period_kind='quarter', fiscal_year=2021,
                    fiscal_quarter=1, quarter_index=8084, fiscal_cycle='2021', fiscal_lineage='calendar')
    normalized = normalize_fact(source, [rule], **metadata)
    assert normalized.status == 'mapped' and normalized.fact.metric == 'revenue'
    assert normalize_fact(replace(source, concept='custom:TotalRevenue'), [rule], **metadata).status == 'unmapped'
    assert normalize_fact(replace(source, scope='separate'), [rule], **metadata).status == 'unmapped'
    assert normalize_fact(replace(source, dimensions={}), [rule], **metadata).status == 'unmapped'


def test_ytd_difference_uses_actual_compatible_input_periods_and_ids():
    from backtest_data.fundamentals.derive import quarter_from_ytd
    current = fact(value='250', quarter=2, kind='ytd')
    prior = fact(value='100', quarter=1, kind='ytd')
    result = quarter_from_ytd(current, prior, as_of=NOW)
    assert result.value == Decimal('150')
    assert result.input_record_ids == (current.id, prior.id)
    assert result.evidence['period_start'] == date(2021, 4, 1)
    assert result.evidence['period_kind'] == 'quarter'


@pytest.mark.parametrize('changes,reason', [({'currency':'KRW'},'currency_mismatch'),
                                        ({'fiscal_cycle':'transition'},'fiscal_cycle_mismatch'),
                                        ({'period_compatible':False},'incompatible_period'),
                                        ({'scope':'separate'},'scope_mismatch'),
                                        ({'available_at':NOW+timedelta(days=1)},'future_or_unobserved_input')])
def test_ytd_difference_rejects_incompatible_or_future_inputs(changes, reason):
    from backtest_data.fundamentals.derive import quarter_from_ytd
    result = quarter_from_ytd(fact(value='250', quarter=2, kind='ytd', **changes), fact(kind='ytd'), as_of=NOW)
    assert result.value is None and result.reason == reason


def test_eps_annual_minus_ytd_is_prohibited():
    from backtest_data.fundamentals.derive import quarter_from_ytd
    result = quarter_from_ytd(fact('eps_diluted', '3', quarter=4, kind='annual'),
                              fact('eps_diluted', '2', quarter=3, kind='ytd'), as_of=NOW)
    assert result.value is None and result.reason == 'eps_requires_reported_quarters'


def test_quarter_sum_requires_four_consecutive_direct_eps_and_preserves_decimal_values():
    from backtest_data.fundamentals.derive import sum_quarters
    quarters = [fact('eps_diluted', value, quarter=q) for q, value in enumerate(('1','.5','.25','.125'), 1)]
    result = sum_quarters(quarters, as_of=NOW)
    assert result.value == Decimal('1.875')
    assert result.evidence['eps_method'] == 'sum_reported_diluted_quarters'
    assert sum_quarters(quarters[:3], as_of=NOW).reason == 'four_compatible_quarters_required'
    assert sum_quarters(quarters[:3]+[replace(quarters[3], directly_reported=False)], as_of=NOW).reason == 'eps_requires_reported_quarters'
    assert sum_quarters(quarters[:3]+[replace(quarters[3], share_basis='other')], as_of=NOW).reason == 'share_basis_mismatch'


@pytest.mark.parametrize('gate', ['captured_at','first_seen_at','extracted_at','decision_recorded_at','materialized_at'])
def test_observed_system_gates_every_real_processing_timestamp(gate):
    from backtest_data.fundamentals.selection import select_facts_asof
    initial = fact(materialized_at=NOW-timedelta(days=10))
    future = replace(initial, **{gate: NOW+timedelta(days=1)})
    assert select_facts_asof([future], as_of=NOW, history_mode='observed_system') == ()
    assert select_facts_asof([future], as_of=NOW, history_mode='reconstructed_public') == (future,)


def test_revision_selection_happens_after_public_visibility_and_requires_observation_times():
    from backtest_data.fundamentals.selection import select_facts_asof
    first = fact()
    correction = replace(first, id='corrected', value=Decimal('110'), revision=1, available_at=NOW+timedelta(days=1))
    assert select_facts_asof([first, correction], as_of=NOW) == (first,)
    assert select_facts_asof([first, correction], as_of=NOW+timedelta(days=2)) == (correction,)
    assert select_facts_asof([replace(first, first_seen_at=None)], as_of=NOW, history_mode='observed_system') == ()


def test_eps_requires_per_share_unit_and_common_attribution_scope():
    from backtest_data.fundamentals.derive import sum_quarters
    quarters=[fact('eps_diluted','1',quarter=q) for q in range(1,5)]
    assert sum_quarters([replace(f,unit='USD') for f in quarters],as_of=NOW).reason=='per_share_unit_required'
    assert sum_quarters([replace(f,scope='consolidated') for f in quarters],as_of=NOW).reason=='common_eps_scope_required'


def test_selection_rejects_unresolved_same_revision_conflict_instead_of_capture_tie_break():
    from backtest_data.fundamentals.selection import AmbiguousFinancialRevision, select_facts_asof
    initial=fact()
    conflict=replace(initial,id='conflict',value=Decimal('999'),captured_at=NOW-timedelta(days=1))
    with pytest.raises(AmbiguousFinancialRevision):
        select_facts_asof([initial,conflict],as_of=NOW)
