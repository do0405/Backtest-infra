from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from test_financial_derivation import NOW, fact


SERIES = ('revenue', 'eps_basic', 'eps_diluted', 'operating_income')


def calculate(metric, facts, **options):
    from backtest_data.fundamentals.ratios import calculate_metric
    return calculate_metric(metric, facts, as_of=NOW, **options)


@pytest.mark.parametrize('series,previous,current,expected', [('revenue','100','120','.2'), ('eps_basic','2','3','.5'),
                                                          ('eps_diluted','2','2.5','.25'), ('operating_income','10','15','.5')])
@pytest.mark.parametrize('comparison', ['qoq','yoy','ttm_yoy'])
def test_required_twelve_growth_ids_select_actual_fiscal_comparisons(series, previous, current, expected, comparison):
    if comparison == 'qoq':
        facts = [fact(series, previous, year=2020, quarter=4), fact(series, current, year=2021, quarter=1)]
    elif comparison == 'yoy':
        facts = [fact(series, previous, year=2020, quarter=1), fact(series, current, year=2021, quarter=1)]
    else:
        facts = [fact(series, previous, year=2020, quarter=q) for q in range(1,5)] + [fact(series, current, year=2021, quarter=q) for q in range(1,5)]
    result = calculate(f'growth_{series}_{comparison}', facts)
    assert result.value == Decimal(expected)
    assert len(result.input_record_ids) == (8 if comparison == 'ttm_yoy' else 2)


@pytest.mark.parametrize('previous,current,status,flag', [('0','5','nonpositive_comparison','from_zero'),
                                                        ('-10','5','nonpositive_comparison','loss_to_profit'),
                                                        ('-10','-5','nonpositive_comparison','loss_narrowed'),
                                                        ('-10','-15','nonpositive_comparison','loss_widened'),
                                                        ('10','-5','ok','profit_to_loss')])
def test_growth_transitions_preserve_delta_without_fabricated_percentage(previous,current,status,flag):
    result = calculate('growth_revenue_qoq', [fact(value=previous, quarter=1), fact(value=current, quarter=2)])
    assert result.status == status and flag in result.flags
    assert result.absolute_delta == Decimal(current)-Decimal(previous)
    assert result.value == (Decimal(current)/Decimal(previous)-1 if Decimal(previous)>0 else None)


def test_roe_roa_use_opening_and_closing_endpoints_with_correct_attribution():
    net_parent = [fact('net_income_parent','5',quarter=q,scope='parent') for q in range(1,5)]
    net_total = [fact('net_income_total','6',quarter=q) for q in range(1,5)]
    balances = [fact('equity_parent','80',year=2020,quarter=4,kind='instant',period_start=None,scope='parent'),
                fact('equity_parent','120',quarter=4,kind='instant',period_start=None,scope='parent'),
                fact('assets_total','200',year=2020,quarter=4,kind='instant',period_start=None),
                fact('assets_total','280',quarter=4,kind='instant',period_start=None)]
    assert calculate('roe_parent_ttm', net_parent+balances).value == Decimal('.2')
    assert calculate('roa_total_ttm', net_total+balances).value == Decimal('.1')
    assert calculate('roe_parent_ttm', net_parent+balances[1:]).reason == 'missing_opening_balance'
    assert calculate('roa_total_ttm', net_parent+balances).value is None


@pytest.mark.parametrize('metric,source,value,expected', [('gross_margin_ttm','gross_profit','40','.4'),
                                                      ('operating_margin_ttm','operating_income','15','.15'),
                                                      ('net_margin_ttm','net_income_total','10','.1')])
def test_ttm_margins_use_same_period_scope(metric,source,value,expected):
    inputs = [fact('revenue','25',quarter=q) for q in range(1,5)] + [fact(source,str(Decimal(value)/4),quarter=q) for q in range(1,5)]
    assert calculate(metric, inputs).value == Decimal(expected)
    assert calculate(metric, [replace(f, scope='separate') if f.metric==source else f for f in inputs]).reason == 'scope_mismatch'


def test_fcf_cash_ppe_and_intangible_capex_no_missing_zero_or_abs_sign():
    from backtest_data.fundamentals.ratios import free_cash_flow
    cfo = fact('cfo','120')
    capex = [fact('cash_capex_ppe','20', sign_policy='positive_outflow'), fact('cash_capex_intangible','10',sign_policy='positive_outflow')]
    assert free_cash_flow(cfo, capex, as_of=NOW).value == Decimal('90')
    assert free_cash_flow(cfo, capex[:1], as_of=NOW).reason == 'missing_cash_capex'
    assert free_cash_flow(cfo, [replace(capex[0],value=Decimal('-20')),capex[1]], as_of=NOW).reason == 'invalid_capex_sign'
    assert free_cash_flow(cfo, [replace(capex[0],value=Decimal('-20'),sign_policy='negative_outflow'),capex[1]], as_of=NOW).value == Decimal('90')
    assert free_cash_flow(cfo, capex+[fact('acquisitions','100')], as_of=NOW).value == Decimal('90')


def test_standard_ebitda_includes_total_net_income_and_rejects_component_overlap():
    from backtest_data.fundamentals.ratios import standard_ebitda
    inputs = [fact('net_income_total','50'), fact('net_income_parent','40',scope='parent'), fact('tax_expense','10'),
              fact('net_interest_expense','5'), fact('depreciation_amortization','20',component_ids=('depreciation','amortization'))]
    assert standard_ebitda(inputs, as_of=NOW).value == Decimal('85')
    assert standard_ebitda(inputs+[fact('depreciation','8',component_ids=('depreciation',))],as_of=NOW).reason == 'overlapping_components'
    assert standard_ebitda(inputs[:-1],as_of=NOW).reason == 'missing_ebitda_component'
    assert standard_ebitda(inputs[1:],as_of=NOW).reason == 'missing_ebitda_component'


def test_annual_family_does_not_substitute_for_ttm_and_rejects_transition_period():
    annual = [fact('eps_diluted','2',year=2020,quarter=4,kind='annual'), fact('eps_diluted','3',year=2021,quarter=4,kind='annual')]
    assert calculate('growth_eps_diluted_fy_yoy',annual).value == Decimal('.5')
    assert calculate('growth_eps_diluted_fy_yoy',[annual[0],replace(annual[1],period_compatible=False)]).reason == 'incompatible_period'
    assert calculate('growth_eps_diluted_ttm_yoy',annual).value is None


def test_growth_does_not_fill_missing_previous_quarter_or_future_revision():
    inputs = [fact(value='100', quarter=1), fact(value='120',quarter=3)]
    assert calculate('growth_revenue_qoq',inputs).reason == 'missing_comparison_period'
    corrected = replace(inputs[0],id='later',value=Decimal('110'),revision=1,available_at=NOW+timedelta(days=1))
    assert calculate('growth_revenue_qoq',[inputs[0],fact(value='120',quarter=2),corrected]).value == Decimal('.2')


def test_observed_calculation_requires_materialized_before_cutoff():
    inputs = [fact(value='100',quarter=1),fact(value='120',quarter=2)]
    assert calculate('growth_revenue_qoq',inputs,history_mode='observed_system',materialized_at=NOW+timedelta(days=1)).reason == 'materialization_after_cutoff'


def test_invalid_average_window_dates_return_unavailable_and_nonpositive_denominator_is_null():
    net_parent=[fact('net_income_parent','5',quarter=q,scope='parent') for q in range(1,5)]
    balances=[fact('equity_parent','-80',year=2020,quarter=4,kind='instant',period_start=None,scope='parent'),
              fact('equity_parent','-120',quarter=4,kind='instant',period_start=None,scope='parent')]
    assert calculate('roe_parent_ttm',[replace(f,period_start=None) for f in net_parent]+balances).reason=='nonconsecutive_periods'
    result=calculate('roe_parent_ttm',net_parent+balances)
    assert result.value is None and result.status=='nonpositive_denominator'


def test_opening_ttm_yoy_uses_warmup_quarters_and_blocks_future_fourth_quarter():
    from datetime import datetime, timezone
    from backtest_data.fundamentals.ratios import calculate_metric
    cutoff=datetime(2016,1,5,tzinfo=timezone.utc)
    public=cutoff-timedelta(days=60)
    inputs=[fact('revenue','100',year=2013,quarter=4,available_at=public,captured_at=NOW)]
    inputs+=[fact('revenue','100' if q<=3 else '120',year=2014,quarter=q,available_at=public,captured_at=NOW) for q in range(1,5)]
    inputs+=[fact('revenue','120',year=2015,quarter=q,available_at=public,captured_at=NOW) for q in range(1,4)]
    future=fact('revenue','9999',year=2015,quarter=4,available_at=cutoff+timedelta(days=60),captured_at=NOW)
    result=calculate_metric('growth_revenue_ttm_yoy',inputs+[future],as_of=cutoff)
    assert result.value==Decimal('.2') and len(result.input_record_ids)==8
    assert future.id not in result.input_record_ids
    assert calculate_metric('growth_revenue_ttm_yoy',inputs[1:]+[future],as_of=cutoff).value is None


def test_ebitda_and_fcf_ttm_margins_use_each_actual_quarter_component():
    inputs=[]
    for q in range(1,5):
        inputs.extend([fact('revenue','100',quarter=q),fact('net_income_total','50',quarter=q),fact('tax_expense','10',quarter=q),
                       fact('net_interest_expense','5',quarter=q),fact('depreciation_amortization','20',quarter=q),
                       fact('cfo','120',quarter=q),fact('cash_capex_ppe','20',quarter=q,sign_policy='positive_outflow'),
                       fact('cash_capex_intangible','10',quarter=q,sign_policy='positive_outflow')])
    assert calculate('ebitda_standard_ttm',inputs).value==Decimal('340')
    assert calculate('ebitda_margin_ttm',inputs).value==Decimal('.85')
    assert calculate('fcf_cfo_less_capex_ttm',inputs).value==Decimal('360')
    assert calculate('fcf_margin_ttm',inputs).value==Decimal('.9')


@pytest.mark.parametrize('metric,components',[
    ('gross_margin_ttm',(('gross_profit','40',None),)),
    ('operating_margin_ttm',(('operating_income','15',None),)),
    ('net_margin_ttm',(('net_income_total','10',None),)),
    ('ebitda_margin_ttm',(('net_income_total','50',None),('tax_expense','10',None),
                          ('net_interest_expense','5',None),('depreciation_amortization','20',None))),
    ('fcf_margin_ttm',(('cfo','120',None),('cash_capex_ppe','20','positive_outflow'),
                       ('cash_capex_intangible','10','positive_outflow'))),
])
def test_margin_numerator_actual_intervals_must_match_revenue_quarters(metric,components):
    inputs=[fact('revenue','100',quarter=q) for q in range(1,5)]
    for q in range(1,5):
        for source,value,sign in components:
            reported=fact(source,value,quarter=q,sign_policy=sign)
            inputs.append(replace(reported,period_start=reported.period_start+timedelta(days=14),
                                  period_end=reported.period_end+timedelta(days=14)))
    result=calculate(metric,inputs)
    assert result.value is None and result.reason=='period_mismatch'
