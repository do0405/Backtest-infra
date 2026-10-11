from datetime import date, datetime, timezone
from decimal import Decimal
import pytest


def test_daily_eligibility_uses_both_high_low_and_volume_rules():
    from backtest_data.prices.vwap_inputs import eligible_daily_trade
    assert eligible_daily_trade('C', ['@', '4']) is True
    assert eligible_daily_trade('C', ['@', 'I']) is False
    assert eligible_daily_trade('A', ['B']) is False
    assert eligible_daily_trade('C', ['B']) is True
    assert eligible_daily_trade('A', ['9']) is False
    with pytest.raises(ValueError, match='condition'):
        eligible_daily_trade('C', ['?'])
    with pytest.raises(ValueError, match='tape'):
        eligible_daily_trade('O', ['@'])


def test_full_day_bounds_and_exact_same_population():
    from backtest_data.prices.vwap_inputs import normalize_trade_page, trade_day_request
    day = date(2020, 8, 28)
    request = trade_day_request('AAPL', day)
    row = {'t': '2020-08-28T14:00:00.000000001Z', 'p': Decimal('100.00001'),
           's': 3, 'z': 'C', 'x': 'V', 'i': 1, 'c': ['@']}
    events, issues = normalize_trade_page({'symbol': 'AAPL', 'trades': [row],
        'next_page_token': None}, symbol='AAPL', session=day)
    assert not issues
    assert events[0]['numerator'] == Decimal('300.00003')
    assert events[0]['quantity'] == 3 and events[0]['eligible']
    assert request['asof'] == '-' and request['feed'] == 'sip'
    excluded = {**row, 'i': 2, 'c': ['@', 'I']}
    events, _ = normalize_trade_page({'symbol': 'AAPL', 'trades': [excluded],
        'next_page_token': None}, symbol='AAPL', session=day)
    assert events[0]['eligible'] is False


def test_foreign_symbol_unknown_and_bad_identity_cannot_certify():
    from backtest_data.prices.vwap_inputs import normalize_trade_page
    events, issues = normalize_trade_page({'symbol': 'OTHER', 'trades': [],
        'next_page_token': None}, symbol='AAPL', session=date(2020, 8, 28))
    assert 'response_symbol_mismatch' in issues
    _, issues = normalize_trade_page({'symbol':'AAPL','trades':[{'c':['?']}],
        'next_page_token':None}, symbol='AAPL', session=date(2020, 8, 28))
    assert issues


def test_query_rejects_intraday_and_future_anchor():
    from backtest_data.prices.vwap_sql import validate_vwap_window
    cutoff = datetime(2026, 10, 7, 18, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match='completed'):
        validate_vwap_window([date(2026,10,7)], cutoff, 'US', None)
    with pytest.raises(ValueError, match='anchor'):
        validate_vwap_window([date(2026,10,6)], cutoff, 'US', cutoff.replace(day=8))


def test_reused_provider_id_at_another_nanosecond_is_distinct():
    from backtest_data.prices.vwap_inputs import normalize_trade_page
    row={'i':2,'x':'D','z':'C','p':Decimal('13.8'),'s':106,'c':['@'],
         't':'2026-10-07T13:30:03.612384776Z'}
    events,issues=normalize_trade_page({'symbol':'ACNT','trades':[row,
        {**row,'t':'2026-10-07T13:30:03.612384777Z'}],'next_page_token':None},
        symbol='ACNT',session=date(2026,10,7))
    assert not issues and events[0]['event_key']!=events[1]['event_key']


def test_equivalent_timestamp_spelling_is_not_a_conflicting_trade():
    from backtest_data.prices.vwap_inputs import normalize_trade_page
    row={'i':2,'x':'D','z':'C','p':Decimal('13.8'),'s':106,'c':['@'],
         't':'2026-10-07T13:30:03.612384776Z'}
    events,_=normalize_trade_page({'symbol':'ACNT','trades':[row,
        {**row,'t':'2026-10-07T13:30:03.612384776+00:00'}],'next_page_token':None},
        symbol='ACNT',session=date(2026,10,7))
    assert events[0]['fingerprint']==events[1]['fingerprint']


def test_future_query_clock_cannot_make_future_sessions_available():
    from backtest_data.prices.vwap_sql import validate_vwap_window
    with pytest.raises(ValueError,match='completed'):
        validate_vwap_window([date(2030,1,1)],datetime(2030,1,3,tzinfo=timezone.utc),'US')


def test_zero_quantity_corrected_close_outside_vwap_population_does_not_poison_day():
    from backtest_data.prices.vwap_inputs import normalize_trade_page
    row={'i':2,'x':'D','z':'B','p':Decimal('13.8'),'s':0,'c':[' ','9'],
         't':'2026-10-07T20:00:03.612384776Z'}
    events,issues=normalize_trade_page({'symbol':'EMFI','trades':[row],'next_page_token':None},
        symbol='EMFI',session=date(2026,10,7))
    assert not issues and len(events)==1 and not events[0]['eligible']
    assert events[0]['numerator']==0
    _,issues=normalize_trade_page({'symbol':'EMFI','trades':[{**row,'c':[' ']}],
        'next_page_token':None},symbol='EMFI',session=date(2026,10,7))
    assert issues


def test_preferred_uses_explicit_same_directory_cqs_alias_without_guessing():
    from backtest_data.prices.vwap_inputs import source_trade_symbol
    member={'market':'US','symbol':'BIP$B','identifier':'nasdaq_directory:BIP$B',
        'fields':{'ACT Symbol':'BIP$B','CQS Symbol':'BIPpB'}}
    assert source_trade_symbol(member)=='BIPpB'
    assert source_trade_symbol({**member,'fields':{}})=='BIP$B'
    with pytest.raises(ValueError,match='binding'):
        source_trade_symbol({**member,'fields':{'ACT Symbol':'OTHER','CQS Symbol':'BIPpB'}})


def test_only_initial_symbol_request_failures_can_advance_as_explicit_gaps():
    from backtest_data.prices.vwap_collection import request_failure_action
    assert request_failure_action('HTTP_400',0)=='record_request_gap'
    assert request_failure_action('HTTP_404',0)=='record_request_gap'
    assert request_failure_action('HTTP_400',1)=='blocked'
    assert request_failure_action('HTTP_403',0)=='blocked'
    assert request_failure_action('HTTP_429',0)=='blocked'
