from decimal import Decimal, Inexact, ROUND_DOWN, ROUND_UP, localcontext
from dataclasses import replace
from datetime import date

from backtest_data.fundamentals.domain import SourceFact
from test_financial_derivation import NOW, fact


def test_normalization_is_exact_and_independent_of_ambient_decimal_context():
    from backtest_data.fundamentals.normalize import MappingRule, normalize_fact
    value=Decimal('123456789012345678901234567890.123456789')
    source=SourceFact('id','X','urn:x','C',value,str(value),'USD','USD',period_end=date(2021,3,31),scope='consolidated')
    rule=MappingRule('urn:x','X','revenue','consolidated',multiplier=Decimal('1'))
    observations=[]
    for precision,rounding in ((9,ROUND_DOWN),(70,ROUND_UP)):
        with localcontext() as context:
            context.prec=precision
            context.rounding=rounding
            context.traps[Inexact]=True
            observations.append(normalize_fact(source,[rule],available_at=NOW,captured_at=NOW).fact)
    assert observations[0].value==observations[1].value==value
    assert observations[0].id==observations[1].id
    assert observations[0].evidence['numeric_policy']=='financial_exact_add_decimal50_half_even_v2'


def test_growth_uses_a_fixed_versioned_decimal_context():
    from backtest_data.fundamentals.ratios import calculate_metric
    inputs=[fact(value='3',quarter=1),fact(value='4',quarter=2)]
    values=[]
    for precision,rounding in ((9,ROUND_DOWN),(70,ROUND_UP)):
        with localcontext() as context:
            context.prec=precision
            context.rounding=rounding
            context.traps[Inexact]=True
            values.append(calculate_metric('growth_revenue_qoq',inputs,as_of=NOW))
    assert values[0].value==values[1].value==Decimal('0.'+'3'*49)
    assert values[0].evidence['numeric_policy']=='financial_exact_add_decimal50_half_even_v2'


def test_parser_fraction_and_scale_ignore_ambient_rounding_and_traps():
    from backtest_data.fundamentals.xbrl import parse_xbrl
    from test_xbrl_extraction import INSTANCE
    scaled=b'<custom:Scaled contextRef="C" unitRef="USD" scale="3" sign="-">123456789012345678901234567890.123456789</custom:Scaled>'
    fraction=b'<custom:Fraction contextRef="C" unitRef="USD"><xbrli:numerator>1</xbrli:numerator><xbrli:denominator>3</xbrli:denominator></custom:Fraction>'
    document=INSTANCE.replace(b'</xbrli:xbrl>',scaled+fraction+b'</xbrli:xbrl>')
    values=[]
    for precision,rounding in ((9,ROUND_DOWN),(70,ROUND_UP)):
        with localcontext() as context:
            context.prec=precision
            context.rounding=rounding
            context.traps[Inexact]=True
            parsed=parse_xbrl(document)
            values.append([f.value for f in parsed.facts[-2:]])
    assert values[0]==values[1]==[Decimal('-123456789012345678901234567890123.456789'),Decimal('0.'+'3'*50)]


def test_every_public_arithmetic_family_uses_the_same_deterministic_policy():
    from backtest_data.fundamentals.basis import ShareBasisAdjustment, rebase_eps
    from backtest_data.fundamentals.derive import quarter_from_ytd, sum_quarters
    from backtest_data.fundamentals.ratios import calculate_metric, free_cash_flow, standard_ebitda
    from backtest_data.fundamentals.validation import check_accounting_identity
    from backtest_data.fundamentals.valuation import CommonShareClass, enterprise_value, ev_ebitda, market_cap_common, pbr_common, per_diluted
    from backtest_data.fundamentals.domain import MetricResult
    from test_valuation import price, shares
    huge='123456789012345678901234567890.123456789'
    quarters=[fact(value=huge,quarter=q) for q in range(1,5)]
    q2=fact(value=huge,quarter=2,kind='ytd')
    q1=fact(value='3',quarter=1,kind='ytd')
    cfo=fact('cfo',huge)
    capex=[fact('cash_capex','3',sign_policy='positive_outflow')]
    components=[fact('net_income_total',huge),fact('tax_expense','3'),fact('net_interest_expense','7'),fact('depreciation_amortization','11')]
    classes=[CommonShareClass('A',price(huge),shares('3'))]
    cap=market_cap_common(classes,required_class_ids={'A'},as_of=NOW)
    eps=[fact('eps_diluted','.75',quarter=q) for q in range(1,5)]
    equity=fact('equity_parent','7',quarter=4,kind='instant',period_start=None,scope='parent')
    preferred=fact('preferred_equity_capital','0',quarter=4,kind='instant',period_start=None,scope='parent')
    ev_pref=replace(preferred,scope='consolidated')
    nci=fact('nci_equity','10',quarter=4,kind='instant',period_start=None)
    cash=fact('cash_equivalents','50',quarter=4,kind='instant',period_start=None)
    debt=fact('interest_bearing_debt','100',quarter=4,kind='instant',period_start=None,evidence={'includes_leases':True})
    ev=enterprise_value(cap,[debt],ev_pref,nci,cash,as_of=NOW)
    ebitda=MetricResult('ebitda_standard_ttm',Decimal('7'),'ok',None,'currency_amount',available_at=NOW,currency='USD',scope='consolidated')
    action=ShareBasisAdjustment('action','old','new',Decimal('3'),NOW,NOW,verified=True)
    total=fact('assets_total',huge,kind='instant',period_start=None,evidence={'source_decimals':'0'})
    liability=fact('liabilities','3',kind='instant',period_start=None,evidence={'source_decimals':'0'})
    def evaluate():
        return (quarter_from_ytd(q2,q1,as_of=NOW).value,sum_quarters(quarters,as_of=NOW).value,
                free_cash_flow(cfo,capex,as_of=NOW).value,standard_ebitda(components,as_of=NOW).value,
                market_cap_common(classes,required_class_ids={'A'},as_of=NOW).value,
                per_diluted(price('4'),eps,as_of=NOW).value,pbr_common(cap,equity,preferred,as_of=NOW).value,
                enterprise_value(cap,[debt],ev_pref,nci,cash,as_of=NOW).value,ev_ebitda(ev,ebitda,as_of=NOW).value,
                rebase_eps(fact('eps_diluted','2',share_basis='old'),action,as_of=NOW).value,
                check_accounting_identity(total,[liability],as_of=NOW).difference)
    results=[]
    for precision,rounding in ((9,ROUND_DOWN),(70,ROUND_UP)):
        with localcontext() as context:
            context.prec=precision
            context.rounding=rounding
            context.traps[Inexact]=True
            results.append(evaluate())
    assert results[0]==results[1] and all(value is not None for value in results[0])


def test_numeric_policy_does_not_inherit_mutable_decimal_default_traps():
    from decimal import DefaultContext
    from backtest_data.fundamentals.ratios import calculate_metric
    saved=dict(DefaultContext.traps)
    try:
        DefaultContext.traps[Inexact]=True
        result=calculate_metric('growth_revenue_qoq',[fact(value='3',quarter=1),fact(value='4',quarter=2)],as_of=NOW)
        assert result.value==Decimal('0.'+'3'*49)
    finally:
        for signal,enabled in saved.items():
            DefaultContext.traps[signal]=enabled


def test_source_sums_ytd_differences_and_fcf_preserve_small_components_exactly():
    from backtest_data.fundamentals.derive import quarter_from_ytd, sum_quarters
    from backtest_data.fundamentals.ratios import free_cash_flow, standard_ebitda
    big='1'+'0'*100
    next_big='1'+'0'*99+'1'
    assert quarter_from_ytd(fact(value=next_big,quarter=2,kind='ytd'),fact(value=big,kind='ytd'),as_of=NOW).value==Decimal('1')
    quarters=[fact(value=big,quarter=1)]+[fact(value='1',quarter=q) for q in (2,3,4)]
    expected=Decimal('1'+'0'*99+'3')
    assert sum_quarters(quarters,as_of=NOW).value==expected
    assert free_cash_flow(fact('cfo',next_big),[fact('cash_capex',big,sign_policy='positive_outflow')],as_of=NOW).value==Decimal('1')
    assert standard_ebitda([fact('net_income_total',big),fact('tax_expense','1'),fact('net_interest_expense','1'),
                            fact('depreciation_amortization','1')],as_of=NOW).value==expected


def test_accepted_4096_digit_source_coefficients_sum_without_rounding_away_carry():
    from backtest_data.fundamentals.derive import sum_quarters
    quarters=[fact(value='9'*4096,quarter=1)]+[fact(value='1',quarter=q) for q in (2,3,4)]
    assert sum_quarters(quarters,as_of=NOW).value==Decimal('1'+'0'*4095+'2')
