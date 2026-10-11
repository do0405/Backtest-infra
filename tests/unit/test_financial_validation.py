from dataclasses import replace
from decimal import Decimal

from test_financial_derivation import NOW, fact


def test_accounting_identity_uses_reported_decimal_uncertainty_and_compatible_scope():
    from backtest_data.fundamentals.validation import check_accounting_identity
    total=fact('assets_total','1000',kind='instant',period_start=None,evidence={'source_decimals':'-3'})
    liabilities=fact('liabilities','700',kind='instant',period_start=None,evidence={'source_decimals':'-3'})
    equity=fact('equity_total','1000',kind='instant',period_start=None,evidence={'source_decimals':'-3'})
    result=check_accounting_identity(total,[liabilities,equity],as_of=NOW)
    assert result.status=='passed' and result.difference==Decimal('-700') and result.tolerance==Decimal('1500')
    assert check_accounting_identity(total,[liabilities,replace(equity,scope='parent')],as_of=NOW).reason=='scope_mismatch'


def test_exact_precision_fails_difference_and_missing_precision_is_not_certified():
    from backtest_data.fundamentals.validation import check_accounting_identity
    total=fact('assets_total','1000',kind='instant',period_start=None,evidence={'source_decimals':'INF'})
    component=fact('liabilities','1000.001',kind='instant',period_start=None,evidence={'source_decimals':'INF'})
    assert check_accounting_identity(total,[component],as_of=NOW).status=='failed'
    assert check_accounting_identity(replace(total,evidence={}),[component],as_of=NOW).reason=='missing_reported_precision'


def test_significant_precision_is_interpreted_at_the_value_magnitude():
    from backtest_data.fundamentals.validation import reported_uncertainty
    assert reported_uncertainty(fact(value='123456789',evidence={'source_precision':'6'}))==Decimal('500')


def test_normalization_scales_source_uncertainty_into_canonical_currency_units():
    from datetime import date
    from backtest_data.fundamentals.domain import SourceFact
    from backtest_data.fundamentals.normalize import MappingRule, normalize_fact
    from backtest_data.fundamentals.validation import check_accounting_identity
    converted=[]
    for concept,metric,value in [('Assets','assets_total','1000'),('Liabilities','liabilities','700'),('Equity','equity_total','301')]:
        source=SourceFact(concept,concept,'urn:ifrs','C',Decimal(value),value,'KRW_thousands','KRW_thousands',
                          period_kind='instant',period_end=date(2021,12,31),scope='consolidated',decimals='0')
        rule=MappingRule('urn:ifrs',concept,metric,'consolidated',source_unit='KRW_thousands',canonical_unit='KRW',
                         multiplier=Decimal('1000'),accounting_basis='IFRS')
        mapped=normalize_fact(source,[rule],available_at=NOW,captured_at=NOW,fiscal_lineage='calendar',
                               fiscal_cycle='2021',fiscal_year=2021).fact
        converted.append(mapped)
    checked=check_accounting_identity(converted[0],converted[1:],as_of=NOW)
    assert checked.status=='passed' and checked.difference==Decimal('-1000') and checked.tolerance==Decimal('1500')
    assert converted[0].evidence['source_decimals']=='0'
    assert converted[0].evidence['source_value']=='1000'
    assert converted[0].evidence['normalization_multiplier']=='1000'


def test_significant_precision_conversion_uses_original_value_and_abs_multiplier():
    from datetime import date
    from backtest_data.fundamentals.domain import SourceFact
    from backtest_data.fundamentals.normalize import MappingRule, normalize_fact
    from backtest_data.fundamentals.validation import reported_uncertainty
    source=SourceFact('V','Value','urn:custom','C',Decimal('123456789'),'123456789','source_units','source_units',
                      period_kind='instant',period_end=date(2021,12,31),scope='consolidated',precision='6')
    for multiplier in (Decimal('3'),Decimal('-3')):
        rule=MappingRule('urn:custom','Value','revenue','consolidated',canonical_unit='KRW',multiplier=multiplier,accounting_basis='IFRS')
        mapped=normalize_fact(source,[rule],available_at=NOW,captured_at=NOW,fiscal_lineage='calendar').fact
        assert reported_uncertainty(mapped)==Decimal('1500')


def test_exact_accounting_identity_never_drops_unit_difference_above_fifty_digits():
    from backtest_data.fundamentals.validation import check_accounting_identity
    total=fact('assets_total','1'+'0'*51,kind='instant',period_start=None,evidence={'source_decimals':'INF'})
    component=fact('liabilities','1'+'0'*50+'1',kind='instant',period_start=None,evidence={'source_decimals':'INF'})
    checked=check_accounting_identity(total,[component],as_of=NOW)
    assert checked.status=='failed' and checked.difference==Decimal('-1') and checked.tolerance==Decimal('0')


def test_accounting_tolerance_comparison_does_not_round_the_absolute_difference():
    from backtest_data.fundamentals.validation import check_accounting_identity
    total=fact('assets_total','1'+'0'*50+'1',kind='instant',period_start=None,evidence={'source_decimals':'-51'})
    component=fact('liabilities','0',kind='instant',period_start=None,evidence={'source_decimals':'-51'})
    checked=check_accounting_identity(total,[component],as_of=NOW)
    assert checked.status=='failed' and checked.difference>checked.tolerance
