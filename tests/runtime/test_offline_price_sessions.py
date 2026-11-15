"""Session retry regressions against the separately built Core runtime."""
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def acquisition(monkeypatch):
    if not (source := os.environ.get('OFFLINE_RUNTIME_SOURCE')):
        pytest.skip('OFFLINE_RUNTIME_SOURCE is required')
    monkeypatch.syspath_prepend(str(Path(source) / 'src'))
    from backtest_data.offline import acquisition
    return acquisition


NOW = datetime(2026, 11, 30, 12, tzinfo=timezone.utc)
MEMBERS = [dict(entity_id='00179df8-84f3-5ed8-9ea6-469a85bf94aa', symbol='A'),
           dict(entity_id='00179df8-84f3-5ed8-9ea6-469a85bf94ab', symbol='B')]


def bar(day, **updates):
    return dict(t=f'2026-11-{day:02}T05:00:00Z', o=10, h=11, l=9, c=10, v=100) | updates


class Bars:
    def __init__(self, *pages):
        self.pages = iter(pages)
        self.calls = []

    def bars(self, symbols, start, end, cursor, anchor):
        self.calls.append((list(symbols), start.isoformat(), end.isoformat(), cursor, anchor))
        bars, token = next(self.pages)
        payload = {'bars': bars, 'next_page_token': token}
        content = json.dumps(payload).encode()
        return SimpleNamespace(content=content, captured_at=NOW, status=200,
                               sha256=sha256(content).hexdigest()), payload


def windows(state, member=MEMBERS[0]):
    return [(row['start'][-2:], row['end'][-2:]) for row in
            state.get('price_gaps', {}).get(member['entity_id'], {}).get('ranges', [])]


def test_primary_waits_for_last_page_and_queues_only_unobserved_open_days(acquisition):
    cycle = dict(start='2026-11-23', end='2026-11-27', offset=0, request_anchor=NOW.isoformat())
    state = {'cycle': cycle}
    client = Bars(({'A': [bar(23), bar(24, c=100)], 'B': [bar(23), bar(24)]}, 'second'),
                  ({'A': [bar(27)], 'B': [bar(25), bar(27)]}, None))
    acquisition._run_primary_price(state, MEMBERS, client, NOW, 'cycle', cycle)
    assert windows(state) == []
    acquisition._run_primary_price(state, MEMBERS, client, NOW, 'cycle', cycle)
    assert windows(state) == [('24', '25')]
    assert windows(state, MEMBERS[1]) == []
    assert [call[3] for call in client.calls] == [None, 'second']


def test_retry_of_legacy_range_retains_only_unobserved_dates(acquisition):
    identity = MEMBERS[0]['entity_id']
    pending = dict(start='2026-11-23', end='2026-11-27', next_retry_at=NOW.isoformat(), attempts=3)
    state = {'price_gaps': {identity: {'symbol': 'A', 'ranges': [pending], 'recovery_audit': []}}}
    client = Bars(({'A': [bar(24), bar(27)]}, None), ({'A': [bar(23)]}, None))
    acquisition._run_price_retry(state, MEMBERS, client, NOW, identity, pending)
    assert windows(state) == [('23', '23'), ('25', '25')]
    first = state['price_gaps'][identity]['ranges'][0]
    acquisition._run_price_retry(state, MEMBERS, client, NOW, identity, first)
    assert client.calls[1][1:3] == ('2026-11-23', '2026-11-23')
    assert windows(state) == [('25', '25')]


def test_new_observation_clears_an_existing_unstarted_gap(acquisition):
    identity = MEMBERS[0]['entity_id']
    cycle = dict(start='2026-11-23', end='2026-11-25', offset=0, request_anchor=NOW.isoformat())
    gap = dict(start='2026-11-23', end='2026-11-25', next_retry_at=NOW.isoformat(), attempts=2)
    state = {'cycle': cycle, 'price_gaps': {identity: {'symbol': 'A', 'start': gap['start'],
             'end': gap['end'], 'ranges': [gap], 'recovery_audit': []}}}
    client = Bars(({'A': [bar(24)], 'B': [bar(23), bar(24), bar(25)]}, None))
    acquisition._run_primary_price(state, MEMBERS, client, NOW, 'cycle', cycle)
    assert windows(state) == [('23', '23'), ('25', '25')]


def directory_member(identity, act, cqs):
    return {'entity_id': identity, 'symbol': cqs, 'source_member': {
        'security_id': identity, 'market': 'US', 'symbol': act,
        'identifier': 'nasdaq_directory:' + act,
        'evidence': {'ACT Symbol': act, 'CQS Symbol': cqs},
    }}


def test_cms_alias_preserves_catalog_identity_and_page_state(acquisition):
    from backtest_data.prices.adapters.alpaca import AlpacaDailyRequest
    member = directory_member('23bc21a6-f81e-5452-9b84-0ad3af596d4f', 'BC$C', 'BCpC')
    cycle = dict(start='2026-11-23', end='2026-11-24', offset=0, request_anchor=NOW.isoformat())
    state = {'cycle': cycle}
    client = Bars(({'BC.PRC': [bar(23)]}, 'next'), ({'BC.PRC': [bar(24)]}, None))
    captures = acquisition._run_primary_price(state, [member], client, NOW, 'cycle', cycle)
    captures += acquisition._run_primary_price(state, [member], client, NOW, 'cycle', cycle)
    assert [call[0] for call in client.calls] == [['BC.PRC'], ['BC.PRC']]
    assert [call[3] for call in client.calls] == [None, 'next']
    assert [capture['entity_id'] for capture, _ in captures] == [member['entity_id']] * 2
    assert [capture['metadata']['symbol'] for capture, _ in captures] == ['BC.PRC'] * 2
    assert windows(state, member) == []
    args = dict(start=date(2026, 11, 23), end=date(2026, 11, 24), captured_at=NOW,
                source_member=member['source_member'])
    assert AlpacaDailyRequest(symbol='BC.PRC', **args).endpoint() == '/v2/stocks/BC.PRC/bars'
    assert AlpacaDailyRequest(symbol='BCpC', **args).endpoint() == '/v2/stocks/BCpC/bars'
    with pytest.raises(ValueError, match='symbol_binding'):
        AlpacaDailyRequest(symbol='BCPC', **args)


def test_legacy_cursor_resumes_the_original_provider_symbol(acquisition):
    from backtest_data.remote.codec import PaginationCursor
    member = directory_member('d91044c9-d724-571c-9d0e-058ce66f8a8a', 'T$C', 'TpC')
    cycle = dict(start='2026-11-23', end='2026-11-24', offset=0, request_anchor=NOW.isoformat(),
                 cursor=PaginationCursor('existing'), active_symbols=['TpC'], seen_symbols=['TpC'])
    state = {'cycle': cycle}
    client = Bars(({'TpC': [bar(24)]}, None))
    acquisition._run_primary_price(state, [member], client, NOW, 'cycle', cycle)
    assert client.calls[0][0] == ['TpC']
    assert client.calls[0][3] == 'existing'


@pytest.mark.parametrize('legacy',[False,True])
def test_new_small_price_batches_and_legacy_cursor_keep_their_members(acquisition,legacy):
    from uuid import UUID
    from backtest_data.remote.codec import PaginationCursor,dumps,loads
    members=[dict(entity_id=str(UUID(int=i+1)),symbol='S'+str(i)) for i in range(105)]
    cycle=dict(start='2026-11-23',end='2026-11-24',offset=0,request_anchor=NOW.isoformat())
    state={'cycle':cycle}
    width=100 if legacy else 24
    if legacy:
        cycle.update(cursor=PaginationCursor('old'),active_symbols=[m['symbol'] for m in members[:width]])
    else:
        acquisition._run_primary_price(state,members,Bars(({},'next')),NOW,'cycle',cycle)
    state=loads(dumps(state))
    client=Bars(({},None),({},None))
    captures=acquisition._run_primary_price(state,members,client,NOW,'cycle',state['cycle'])
    assert len(captures)==width
    assert client.calls[0][0]==[m['symbol'] for m in members[:width]]
    assert client.calls[0][3]==('old' if legacy else 'next')
    assert state['cycle']['offset']==width
    acquisition._run_primary_price(state,members,client,NOW,'cycle',state['cycle'])
    assert client.calls[1][0]==[m['symbol'] for m in members[width:width+24]]


def test_dart_instance_budget_resumes_after_serializing_committed_state(acquisition,monkeypatch):
    from backtest_data.offline.contracts import canonical_bytes,make_capture
    from backtest_data.remote.codec import dumps,loads
    member=MEMBERS[0]
    catalog=dict(output_end='2026-10-07',target_count=17758,catalog_sha256='a'*64,dart_issuers=[member])
    state=acquisition.new_state(catalog)
    filing=dict(member=member,filing=dict(rcept_no='20261115000001',report_nm='분기보고서'))
    state['sources']['opendart']['cycle']=dict(start='2026-11-01',end='2026-11-20',offset=1,pending=[filing])
    calls=[]
    bodies=[canonical_bytes(dict(version=1,format='offline_extraction_v1',source_content_hash=c*64,result={}))
        for c in ('a','b')]
    def fetch(item,client):
        calls.append(item['filing']['rcept_no'])
        return [(make_capture(source='opendart',kind='dart_extraction',entity_id=member['entity_id'],
            captured_at=NOW,content=body,metadata={'instance_key':instance}),body)
            for instance,body in zip(('structured:CFS','structured:OFS'),bodies)]
    monkeypatch.setattr(acquisition,'_dart_filing',fetch)
    clients={'opendart':object()}
    state, first=acquisition.acquire_tick(catalog,state,clients,now=NOW,max_steps=2,max_capture_bytes=len(bodies[0]))
    assert state['provider_status']['opendart']['status']=='storage_deferred'
    assert state['sources']['opendart']['cycle']['pending'][0]['dart_instances']['next']==1
    state, second=acquisition.acquire_tick(catalog,loads(dumps(state)),clients,now=NOW,max_steps=1,max_capture_bytes=len(bodies[0]))
    assert 'cycle' not in state['sources']['opendart']
    assert state['sources']['opendart']['watermark']=='2026-11-20'
    assert [capture['metadata']['instance_key'] for capture,_ in first+second]==['structured:CFS','structured:OFS']
    assert calls==['20261115000001']*2


def test_dated_skk_alias_requires_the_exact_issue_and_effective_day(acquisition):
    from backtest_data.prices.adapters.alpaca import AlpacaDailyRequest

    skk = directory_member('8082ceac-1826-5e03-948d-0766c11e4e00', 'SKK', 'SKK')
    assert acquisition._member_price_routes(skk, '2026-10-12', '2026-10-13') == [
        ('2026-10-12', '2026-10-12', 'SKK'),
        ('2026-10-13', '2026-10-13', 'RNTZ'),
    ]
    unrelated = directory_member('23bc21a6-f81e-5452-9b84-0ad3af596d4f', 'SKK', 'SKK')
    assert acquisition._member_price_routes(unrelated, '2026-10-12', '2026-10-13') == [
        ('2026-10-12', '2026-10-13', 'SKK'),
    ]
    args = dict(symbol='RNTZ', source_member=skk['source_member'], captured_at=NOW, end=date(2026, 10, 13))
    assert AlpacaDailyRequest(start=date(2026, 10, 13), **args).symbol == 'RNTZ'
    with pytest.raises(ValueError, match='symbol_binding'):
        AlpacaDailyRequest(start=date(2026, 10, 12), **args)


def test_reused_ticker_has_disjoint_original_and_new_identity_windows(acquisition):
    from backtest_data.prices.adapters.alpaca import AlpacaDailyRequest
    indv = directory_member('e44a34a4-38a0-5960-8480-e6f9b185581a', 'INDV', 'INDV')
    old_supn = directory_member('fe76957b-e176-58f5-bd0c-9388da2a2de6', 'SUPN', 'SUPN')
    cycle = dict(start='2026-11-02', end='2026-11-04', offset=0, request_anchor=NOW.isoformat())
    state = {'cycle': cycle}
    client = Bars(({'INDV': [bar(2)]}, None), ({'SUPN': [bar(3), bar(4)]}, None))
    assert acquisition._run_primary_price(state, [indv, old_supn], client, NOW, 'cycle', cycle) == []
    assert windows(state, old_supn) == []
    pending = state['price_gaps'][indv['entity_id']]['ranges'][0]
    acquisition._run_price_retry(state, [indv, old_supn], client, NOW, indv['entity_id'], pending)
    for pending in list(state['price_gaps'][indv['entity_id']]['ranges']):
        acquisition._run_price_retry(state, [indv, old_supn], client, NOW, indv['entity_id'], pending)
    assert [call[:3] for call in client.calls] == [
        (['INDV'], '2026-11-02', '2026-11-02'),
        (['SUPN'], '2026-11-03', '2026-11-04'),
    ]
    args = dict(symbol='SUPN', source_member=indv['source_member'], captured_at=NOW,
                end=date(2026, 11, 4))
    assert AlpacaDailyRequest(start=date(2026, 11, 3), **args).symbol == 'SUPN'
    with pytest.raises(ValueError, match='symbol_binding'):
        AlpacaDailyRequest(start=date(2026, 11, 2), **args)


def test_replay_does_not_store_old_supn_issue_after_ticker_reuse(acquisition, monkeypatch, tmp_path):
    from backtest_data.offline import replay

    member = directory_member('fe76957b-e176-58f5-bd0c-9388da2a2de6', 'SUPN', 'SUPN')
    capture = {'entity_id': member['entity_id'], 'content_sha256': 'a' * 64, 'metadata': {
        'symbol': 'SUPN', 'start': '2026-10-30', 'end': '2026-11-04', 'request_anchor': NOW.isoformat(),
        'feed': 'sip', 'adjustment': 'raw', 'asof': '-', 'source_member': member['source_member'],
        'bulk_endpoint': '/v2/stocks/bars', 'bulk_symbols': ['SUPN'],
    }}
    old_bar = bar(1, t='2026-10-30T04:00:00Z')
    content = json.dumps({'bars': {'SUPN': [old_bar, bar(3)]}, 'next_page_token': None}).encode()
    security = SimpleNamespace(id=member['entity_id'], market='US', source_identifier='nasdaq_directory:SUPN')
    session = SimpleNamespace(get=lambda model, identity: security)

    def store(session, identity, request, response, payload, path, **context):
        assert identity == member['entity_id']
        assert payload['bars'] == [old_bar]
        assert response.content == content
        return {'response_valid': True, 'pagination_valid': True, 'inserted': 1,
                'source_row_count': 1, 'rejected': 0, 'normalized_row_coverage_complete': True}

    monkeypatch.setattr(replay, 'store_alpaca_prices', store)
    replay._adopt_alpaca(session, capture, content, NOW, tmp_path)
