from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from backtest_data.fundamentals.domain import MetricResult
from test_financial_derivation import NOW, fact


def price(value='60', **changes):
    from backtest_data.fundamentals.valuation import ValuationPrice
    fields=dict(id='price:A',security_id='security:A',listing_id='listing:A',value=Decimal(value),currency='USD',
                share_basis='split:2022',available_at=NOW-timedelta(days=1),captured_at=NOW-timedelta(days=1),
                decision_recorded_at=NOW-timedelta(days=1))
    fields.update(changes)
    return ValuationPrice(**fields)


def shares(value='10', **changes):
    return fact('shares_outstanding_common',value,quarter=4,kind='instant',period_start=None,scope='parent_common',
                unit='shares',share_basis='split:2022',**changes)


def capitalization():
    from backtest_data.fundamentals.valuation import CommonShareClass, market_cap_common
    return market_cap_common([CommonShareClass('A',price(),shares())],required_class_ids={'A'},as_of=NOW)


def test_cap_allclasses_pit_outstanding_and_listing_deduplication():
    from backtest_data.fundamentals.valuation import CommonShareClass, market_cap_common
    first=CommonShareClass('A',price(),shares())
    duplicate=CommonShareClass('A',price(listing_id='other_listing'),shares(),is_primary_listing=False)
    cap=market_cap_common([first,duplicate],required_class_ids={'A'},as_of=NOW)
    assert cap.value == Decimal('600')
    assert set(cap.input_record_ids)=={'price:A',shares().id}
    assert market_cap_common([first],required_class_ids={'A','B'},as_of=NOW).reason=='capitalization_coverage_incomplete'
    assert market_cap_common([replace(first,shares=replace(shares(),metric='weighted_average_diluted_shares'))],required_class_ids={'A'},as_of=NOW).reason=='pit_outstanding_shares_required'
    assert market_cap_common([replace(first,shares=replace(shares(),available_at=NOW+timedelta(days=1)))],required_class_ids={'A'},as_of=NOW).value is None


@pytest.mark.parametrize('changes,reason', [({'basis':'total_return'},'raw_price_required'),
                                         ({'share_basis':'unverified'},'share_basis_mismatch'),
                                         ({'available_at':NOW+timedelta(days=1)},'future_or_unobserved_price')])
def test_cap_rejects_price_basis_and_future_inputs(changes,reason):
    from backtest_data.fundamentals.valuation import CommonShareClass, market_cap_common
    result=market_cap_common([CommonShareClass('A',price(**changes),shares())],required_class_ids={'A'},as_of=NOW)
    assert result.value is None and result.reason==reason


def test_per_strict_reported_quarters_and_separate_annual_family():
    from backtest_data.fundamentals.valuation import per_diluted
    quarters=[fact('eps_diluted','.75',quarter=q) for q in range(1,5)]
    assert per_diluted(price(),quarters,as_of=NOW).value==Decimal('20')
    annual=[fact('eps_diluted','3',quarter=4,kind='annual')]
    assert per_diluted(price(),annual,as_of=NOW).value is None
    result=per_diluted(price(),annual,as_of=NOW,annual=True)
    assert result.value==Decimal('20') and result.metric_id=='per_diluted_fy'
    assert result.definition_version=='financial_metrics_annual_v1'
    assert per_diluted(price(),[replace(f,value=Decimal('-1')) for f in quarters],as_of=NOW).reason=='nonpositive_eps'


def test_common_pbr_excludes_equity_classified_preferred_and_does_not_assume_missing_zero():
    from backtest_data.fundamentals.valuation import pbr_common
    equity=fact('equity_parent','320',quarter=4,kind='instant',period_start=None,scope='parent')
    preferred=fact('preferred_equity_capital','20',quarter=4,kind='instant',period_start=None,scope='parent')
    assert pbr_common(capitalization(),equity,preferred,as_of=NOW).value==Decimal('2')
    assert pbr_common(capitalization(),equity,None,as_of=NOW).reason=='missing_preferred_equity'


def test_ev_book_basis_and_multiple_with_explicit_leases_and_no_duplicate_totals():
    from backtest_data.fundamentals.valuation import enterprise_value, ev_ebitda
    debt=fact('interest_bearing_debt','100',quarter=4,kind='instant',period_start=None,evidence={'includes_leases':True})
    preferred=fact('preferred_equity_capital','20',quarter=4,kind='instant',period_start=None)
    nci=fact('nci_equity','10',quarter=4,kind='instant',period_start=None)
    cash=fact('cash_equivalents','50',quarter=4,kind='instant',period_start=None)
    ev=enterprise_value(capitalization(),[debt],preferred,nci,cash,as_of=NOW)
    assert ev.value==Decimal('680') and ev.evidence['preferred_nci_basis']=='book_basis'
    ebitda=MetricResult('ebitda_standard_ttm',Decimal('85'),'ok',None,'currency_amount',('ebitda:1',),NOW-timedelta(days=1),currency='USD',scope='consolidated')
    assert ev_ebitda(ev,ebitda,as_of=NOW).value==Decimal('8')
    duplicate=replace(debt,id='lease',metric='lease_debt',value=Decimal('10'))
    assert enterprise_value(capitalization(),[debt,duplicate],preferred,nci,cash,as_of=NOW).reason=='overlapping_debt_components'
    assert enterprise_value(capitalization(),[replace(debt,evidence={})],preferred,nci,cash,as_of=NOW).reason=='lease_coverage_unverified'
    assert ev_ebitda(ev,replace(ebitda,value=Decimal('0')),as_of=NOW).reason=='nonpositive_ebitda'


def test_unverified_fx_is_blocked_and_verified_visible_fx_carries_lineage():
    from backtest_data.fundamentals.valuation import CommonShareClass, FXRate, market_cap_common
    classes=[CommonShareClass('A',price(currency='KRW'),shares())]
    assert market_cap_common(classes,required_class_ids={'A'},as_of=NOW,target_currency='USD').reason=='currency_mismatch'
    rate=FXRate('fx:1','KRW','USD',Decimal('.001'),NOW-timedelta(days=1),NOW-timedelta(days=1),verified=True,definition_version='fx_v1')
    converted=market_cap_common(classes,required_class_ids={'A'},as_of=NOW,target_currency='USD',fx_rates=[rate])
    assert converted.value==Decimal('.600') and 'fx:1' in converted.input_record_ids
    assert market_cap_common(classes,required_class_ids={'A'},as_of=NOW,target_currency='USD',fx_rates=[replace(rate,available_at=NOW+timedelta(days=1))]).reason=='currency_mismatch'


def test_explicit_split_rebases_eps_and_preserves_action_inputs():
    from backtest_data.fundamentals.basis import ShareBasisAdjustment, rebase_eps
    eps=fact('eps_diluted','2',share_basis='before_split')
    adjustment=ShareBasisAdjustment('split:1','before_split','split:2022',Decimal('2'),NOW-timedelta(days=1),NOW-timedelta(days=1),verified=True)
    normalized=rebase_eps(eps,adjustment,as_of=NOW)
    assert normalized.value==Decimal('1') and normalized.share_basis=='split:2022'
    assert normalized.input_ids==(eps.id,'split:1')
    with pytest.raises(ValueError,match='unverified_or_future_share_basis'):
        rebase_eps(eps,replace(adjustment,available_at=NOW+timedelta(days=1)),as_of=NOW)


def test_ev_can_use_verified_fx_for_financial_components_and_exposes_negative_ev():
    from backtest_data.fundamentals.valuation import FXRate, enterprise_value
    debt=fact('interest_bearing_debt','100000',quarter=4,kind='instant',period_start=None,unit='KRW',currency='KRW',evidence={'includes_leases':True})
    preferred=fact('preferred_equity_capital','20',quarter=4,kind='instant',period_start=None)
    nci=fact('nci_equity','10',quarter=4,kind='instant',period_start=None)
    cash=fact('cash_equivalents','1000',quarter=4,kind='instant',period_start=None)
    fx=FXRate('fx:1','KRW','USD',Decimal('.001'),NOW-timedelta(days=1),NOW-timedelta(days=1),verified=True,definition_version='fx_v1')
    assert enterprise_value(capitalization(),[debt],preferred,nci,cash,as_of=NOW).reason=='currency_mismatch'
    result=enterprise_value(capitalization(),[debt],preferred,nci,cash,as_of=NOW,fx_rates=[fx])
    assert result.value==Decimal('-270') and 'negative_ev' in result.flags and 'fx:1' in result.input_record_ids


def test_rebased_fact_carries_action_adoption_gate_into_earlier_observed_queries():
    from datetime import datetime, timezone
    from backtest_data.fundamentals.basis import ShareBasisAdjustment, rebase_eps, rebase_outstanding_shares
    from backtest_data.fundamentals.selection import select_facts_asof
    may1=datetime(2022,5,1,tzinfo=timezone.utc)
    may12=datetime(2022,5,12,tzinfo=timezone.utc)
    may27=datetime(2022,5,27,tzinfo=timezone.utc)
    action=ShareBasisAdjustment('split:late-adoption','before_split','split:2022',Decimal('2'),
                                may1,may1,verified=True,decision_recorded_at=may27)
    inputs=[fact('eps_diluted','2',share_basis='before_split'),replace(shares(),share_basis='before_split')]
    for original,rebase in zip(inputs,(rebase_eps,rebase_outstanding_shares)):
        rebased=rebase(original,action,as_of=NOW,history_mode='observed_system')
        assert rebased.decision_recorded_at==may27
        assert select_facts_asof([rebased],as_of=may12,history_mode='observed_system')==()
        assert select_facts_asof([rebased],as_of=NOW,history_mode='observed_system')==(rebased,)
        assert original.decision_recorded_at<may12
        unknown_decision=rebase(original,replace(action,decision_recorded_at=None),as_of=NOW)
        assert unknown_decision.decision_recorded_at is None
        assert select_facts_asof([unknown_decision],as_of=NOW,history_mode='observed_system')==()


def test_ev_rejects_lease_details_already_in_current_and_noncurrent_debt():
    from backtest_data.fundamentals.valuation import enterprise_value
    debt=[fact('interest_debt_current','40',quarter=4,kind='instant',period_start=None,evidence={'includes_leases':True}),
          fact('interest_debt_noncurrent','60',quarter=4,kind='instant',period_start=None,evidence={'includes_leases':True}),
          fact('lease_debt_current','5',quarter=4,kind='instant',period_start=None),
          fact('lease_debt_noncurrent','15',quarter=4,kind='instant',period_start=None)]
    preferred=fact('preferred_equity_capital','20',quarter=4,kind='instant',period_start=None)
    nci=fact('nci_equity','10',quarter=4,kind='instant',period_start=None)
    cash=fact('cash_equivalents','50',quarter=4,kind='instant',period_start=None)
    result=enterprise_value(capitalization(),debt,preferred,nci,cash,as_of=NOW)
    assert result.value is None and result.reason=='overlapping_debt_components'
    separate=[replace(item,evidence={'includes_leases':False}) if item.metric.startswith('interest_debt_') else item for item in debt]
    assert enterprise_value(capitalization(),separate,preferred,nci,cash,as_of=NOW).value==Decimal('700')
    assert enterprise_value(capitalization(),debt[:2],preferred,nci,cash,as_of=NOW).value==Decimal('680')


def test_common_capitalization_rejects_known_different_share_fact_releases():
    from backtest_data.fundamentals.valuation import CommonShareClass, market_cap_common
    classes=[CommonShareClass('A',price(),replace(shares(),release_id='R1')),
             CommonShareClass('B',price(id='price:B',security_id='security:B',listing_id='listing:B'),replace(shares(),id='shares:B',release_id='R2'))]
    result=market_cap_common(classes,required_class_ids={'A','B'},as_of=NOW)
    assert result.value is None and result.reason=='release_mismatch'
    unbound=[replace(item,shares=replace(item.shares,release_id=None)) for item in classes]
    assert market_cap_common(unbound,required_class_ids={'A','B'},as_of=NOW).value==Decimal('1200')
