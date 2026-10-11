from dataclasses import replace
from datetime import date,datetime,timedelta,timezone
from decimal import Decimal as D,localcontext
import pytest
from backtest_data.prices.provider_basis import classify_price_basis
from backtest_data.prices.validation import ComparisonContract

STAMP=datetime(2026,10,10,12,tzinfo=timezone.utc)


def candidate(identity='a',**changes):
    from backtest_data.prices.reconciliation import SupplierPriceCandidate
    definition=classify_price_basis('alpaca','/v2/stocks/AAPL/bars',{'adjustment':'split'})
    base=dict(id=identity,security_id='original-security',market='US',session=date(2016,1,4),currency='USD',feed='sip',
        venue='US_CONSOLIDATED',session_scope='new_york_calendar_day',trade_scope='daily_condition_filtered',
        price_unit='currency_per_share',quantity_unit='shares',source='alpaca',source_contract_id='contract-'+identity,
        raw_ref='raw-'+identity,definition=definition,reference_basis='current:2026-10-10',reference_evidence_ref='declared-reference',
        open=D('10'),high=D('12'),low=D('9'),close=D('11'),volume=D('100'),captured_at=STAMP,available_at=STAMP,
        upstream_id=None,processing_path=None)
    return SupplierPriceCandidate(**(base|changes))


def contract(**changes):
    return replace(ComparisonContract('verified USD rounding v1',tick_size=D('0.01')),**changes)


def reconcile(rows,**kwargs):
    from backtest_data.prices.reconciliation import reconcile_supplier_prices
    return reconcile_supplier_prices(rows,as_of=STAMP,recorded_at=STAMP+timedelta(hours=1),contract=contract(),**kwargs)


def test_unspecified_kis_adjustment_is_incomparable_to_alpaca_split_even_equal_prices():
    from backtest_data.prices.reconciliation import compare_supplier_prices
    left=candidate();right=candidate('b',source='kis_overseas',definition=classify_price_basis('kis_overseas',
        '/uapi/overseas-price/v1/quotations/dailyprice',{'MODP':'1'}))
    result=compare_supplier_prices(left,right,as_of=STAMP,contract=contract())
    assert result.status=='incomparable'
    assert 'adjustment_definition_mismatch' in {issue.code for issue in result.issues}


@pytest.mark.parametrize('changes',[{'currency':'KRW'},{'feed':'iex'},{'session_scope':'regular_only'},
    {'reference_basis':'current:2026-10-09'},{'reference_basis':None},{'session_scope':None}])
def test_incompatible_or_unknown_reference_context_never_corroborates(changes):
    from backtest_data.prices.reconciliation import compare_supplier_prices
    assert compare_supplier_prices(candidate(),candidate('b',**changes),as_of=STAMP,contract=contract()).status=='incomparable'


def test_single_declared_adjusted_value_needs_no_complete_action_ledger_or_verified_reference():
    result=reconcile([candidate(reference_basis=None,reference_evidence_ref=None)])
    assert result.decision.status=='accepted' and result.decision.selected_observation_id=='a'
    assert result.declaration_grade=='supplier_declared'
    assert result.corroboration_grade=='not_corroborated'


def test_two_agreeing_values_nominate_existing_value_deterministically_without_average():
    first,second=candidate(),candidate('b',close=D('11.005'))
    a=reconcile([second,first]);b=reconcile([first,second])
    assert a.decision.selected_observation_id==b.decision.selected_observation_id=='a'
    assert a.corroboration_grade=='agreement_independence_unknown'
    assert first.close==D('11') and second.close==D('11.005')


def test_explicit_independent_processing_is_distinct_from_unknown_and_shared_origin():
    from backtest_data.prices.reconciliation import compare_supplier_prices
    left=candidate(upstream_id='SIP',processing_path='path-one');right=candidate('b',upstream_id='SIP',processing_path='path-two')
    known=compare_supplier_prices(left,right,as_of=STAMP,contract=contract(independent_paths_verified=True))
    assert known.independence=='independent_processing_shared_origin'
    assert compare_supplier_prices(left,right,as_of=STAMP,contract=contract()).independence=='unknown'
    duplicate=compare_supplier_prices(left,replace(right,raw_ref=left.raw_ref),as_of=STAMP,contract=contract(independent_paths_verified=True))
    assert duplicate.independence=='same_capture'


def test_unique_two_of_three_group_nominates_existing_value_and_records_dissent():
    result=reconcile([candidate(),candidate('b'),candidate('c',close=D('11.03'))])
    assert result.decision.status=='accepted'
    assert result.decision.selected_observation_id=='a'
    assert result.agreeing_observation_ids==('a','b')
    assert result.dissenting_observation_ids==('c',)


def test_overlapping_nontransitive_agreement_does_not_silently_choose_middle():
    result=reconcile([candidate(),candidate('b',close=D('11.009')),candidate('c',close=D('11.018'))])
    assert result.decision.status=='quarantined'
    assert result.decision.selected_observation_id is None


def test_rounding_requires_explicit_quanta_and_decimal_comparison_ignores_ambient_context():
    from backtest_data.prices.reconciliation import compare_supplier_prices
    left=candidate(close=D('11'));right=candidate('b',close=D('11.005'))
    assert compare_supplier_prices(left,right,as_of=STAMP,contract=contract(tick_size=None)).status=='unresolved'
    rounding=contract(tick_size=None,left_price_quantum=D('0.01'),right_price_quantum=D('0.001'))
    with localcontext() as context:
        context.prec=3
        assert compare_supplier_prices(left,right,as_of=STAMP,contract=rounding).status=='agree'
        assert compare_supplier_prices(left,replace(right,close=D('11.006')),as_of=STAMP,contract=rounding).status=='mismatch'


def test_uncompleted_session_or_future_query_cutoff_never_selects_data():
    assert reconcile([candidate(session=STAMP.date())]).decision.status=='unavailable'
    from backtest_data.prices.reconciliation import reconcile_supplier_prices
    with pytest.raises(ValueError,match='future'):
        reconcile_supplier_prices([candidate()],as_of=STAMP+timedelta(days=2),recorded_at=STAMP,contract=contract())


def test_late_capture_does_not_apply_price_capture_pit_to_completed_market_sessions():
    from backtest_data.prices.reconciliation import reconcile_supplier_prices
    cutoff=datetime(2026,10,8,4,tzinfo=timezone.utc)
    split=candidate(session=date(2026,10,7),reference_basis=None,reference_evidence_ref=None)
    raw=candidate('b',source='kis_overseas',session=date(2026,10,7),feed='kis:us:AMS',venue='AMS',
        definition=classify_price_basis('kis_overseas','/uapi/overseas-price/v1/quotations/dailyprice',{'MODP':'0'}))
    result=reconcile_supplier_prices([split,raw],as_of=cutoff,recorded_at=STAMP+timedelta(hours=1),
        contract=contract(),candidate_id=split.id)
    assert result.decision.status=='accepted' and result.decision.selected_observation_id==split.id
    assert result.comparisons[0].status=='incomparable' and result.corroboration_grade=='not_corroborated'
    assert split.captured_at==split.available_at==STAMP


def test_physical_audit_recording_clock_cannot_precede_input_capture():
    from backtest_data.prices.reconciliation import reconcile_supplier_prices
    with pytest.raises(ValueError,match='recording.*capture'):
        reconcile_supplier_prices([candidate(captured_at=STAMP+timedelta(hours=2))],as_of=STAMP,
            recorded_at=STAMP+timedelta(hours=1),contract=contract())


def test_latest_visible_revision_is_used_and_same_capture_conflict_is_not_silently_broken():
    old=candidate('old',source_contract_id='same-contract',captured_at=STAMP-timedelta(days=1),available_at=STAMP-timedelta(days=1),close=D('11.5'))
    current=candidate('new',source_contract_id='same-contract')
    result=reconcile([old,current,candidate('peer')])
    assert result.decision.selected_observation_id in {'new','peer'}
    assert result.agreeing_observation_ids==('new','peer')
    assert result.excluded_observation_ids==('old',)
    assert reconcile([current,replace(current,id='conflict',close=D('11.4'))]).decision.status=='quarantined'


def test_two_disagreeing_or_disjoint_agreement_groups_are_quarantined():
    assert reconcile([candidate(),candidate('b',close=D('11.1'))]).decision.status=='quarantined'
    assert reconcile([candidate(),candidate('b'),candidate('c',close=D('11.1')),
        candidate('d',close=D('11.1'))]).decision.status=='quarantined'


def test_declared_source_priority_nominates_an_existing_raw_value():
    alpaca=candidate('alpaca',definition=classify_price_basis('alpaca','/v2/stocks/AAPL/bars',{'adjustment':'raw'}))
    kis=candidate('kis',source='kis_overseas',close=D('11.005'),definition=classify_price_basis(
        'kis_overseas','/uapi/overseas-price/v1/quotations/dailyprice',{'MODP':'0'}))
    result=reconcile([alpaca,kis],source_priority=('kis_overseas','alpaca'))
    assert result.decision.selected_observation_id=='kis'
    assert result.corroboration_grade=='agreement_independence_unknown'
    assert kis.close==D('11.005')


@pytest.mark.parametrize('changes',[{'reference_basis':'unknown'},{'session_scope':'unknown'},
    {'trade_scope':'supplier_daily_eligibility_unverified'}])
def test_explicit_unknown_sentinels_do_not_become_same_definition_by_equal_strings(changes):
    from backtest_data.prices.reconciliation import compare_supplier_prices
    assert compare_supplier_prices(candidate(**changes),candidate('b',**changes),as_of=STAMP,contract=contract()).status=='incomparable'


def test_distinct_declared_adjustment_references_are_not_discarded_as_stale_revisions():
    old=candidate('old',source_contract_id='same-contract',reference_basis='as_of:2026-10-09',
        captured_at=STAMP-timedelta(days=1),available_at=STAMP-timedelta(days=1))
    new=candidate('new',source_contract_id='same-contract',reference_basis='as_of:2026-10-10')
    result=reconcile([old,new])
    assert result.comparisons[0].status=='incomparable'
    assert result.excluded_observation_ids==()


def test_sql_adapter_rejects_units_currency_or_feed_contradicting_explicit_contract():
    from types import SimpleNamespace
    from backtest_data.prices.reconciliation_sql import candidate_from_sql
    row=SimpleNamespace(**{field:getattr(candidate(),field) for field in ('id','security_id','source_contract_id',
        'session','currency','feed','venue','trade_scope','quantity_unit','open','high','low','close','volume','captured_at','available_at')},
        basis='provider_adjusted',raw_id='raw-a',evidence={})
    security=SimpleNamespace(id=row.security_id,market='US')
    for key,value in [('currency','EUR'),('feed','iex'),('quantity_unit','lots')]:
        source=SimpleNamespace(id=row.source_contract_id,source='alpaca',domain='prices',market='US',
            specification={'endpoint':'/v2/stocks/{symbol}/bars','request_parameters':{'adjustment':'split'},key:value})
        with pytest.raises(ValueError,match='contradicts'):candidate_from_sql(row,source,security)
