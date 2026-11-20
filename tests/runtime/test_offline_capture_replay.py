from datetime import datetime, timezone
import os
from pathlib import Path

import pytest


@pytest.fixture
def acquisition(monkeypatch):
    source = os.environ.get('OFFLINE_RUNTIME_SOURCE')
    if not source:
        pytest.skip('OFFLINE_RUNTIME_SOURCE required')
    monkeypatch.syspath_prepend(str(Path(source)/'src'))
    from backtest_data.offline import acquisition
    return acquisition


def test_exact_cached_capture_replay_can_advance_cursor_without_duplicate_publication(acquisition, monkeypatch):
    from backtest_data.offline.contracts import canonical_bytes, make_capture
    from backtest_data.offline.archives import build_bundles, read_bundle
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    member = {'entity_id': '00179df8-84f3-5ed8-9ea6-469a85bf94aa'}
    catalog = dict(output_end='2026-10-07', target_count=17758, catalog_sha256='a'*64, dart_issuers=[member])
    content = canonical_bytes(dict(version=1, format='offline_extraction_v1', source_content_hash='b'*64, result={}))
    capture = make_capture(source='opendart', kind='dart_extraction', entity_id=member['entity_id'],
                           captured_at=now, content=content, metadata={'instance_key': 'structured:CFS'})
    def replay(state, *args, **kwargs):
        state['turns'] = state.get('turns', 0)+1
        return [(dict(capture), bytes(bytearray(content)))]
    monkeypatch.setattr(acquisition, '_dart_step', replay)
    state, captures = acquisition.acquire_tick(catalog, None, {'opendart': object()}, now=now, max_steps=2)
    assert state['sources']['opendart']['turns'] == 2
    assert len(captures) == 1
    bundles = build_bundles(captures)
    assert list(read_bundle(bundles[0].data)) == [(capture, content)]


def test_distinct_observation_clock_is_not_deduplicated(acquisition, monkeypatch):
    from backtest_data.offline.contracts import make_capture
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    member = {'entity_id': '00179df8-84f3-5ed8-9ea6-469a85bf94aa'}
    catalog = dict(output_end='2026-10-07', target_count=17758, catalog_sha256='a'*64, us_prices=[member])
    observations = iter([now, now.replace(second=1)])
    def capture(*args, **kwargs):
        body = b'{"bars":{}}'
        value = make_capture(source='alpaca', kind='alpaca_bars', entity_id=member['entity_id'],
                             captured_at=next(observations), content=body, metadata={})
        return [(value, body)]
    monkeypatch.setattr(acquisition, '_alpaca_step', capture)
    _, captures = acquisition.acquire_tick(catalog, None, {'alpaca': object()}, now=now, max_steps=2)
    assert len(captures) == 2 and captures[0][0]['capture_id'] != captures[1][0]['capture_id']


def test_replayed_identity_still_checks_original_content_hash(acquisition, monkeypatch):
    from backtest_data.offline.contracts import make_capture
    now=datetime(2026,11,20,tzinfo=timezone.utc)
    member={'entity_id':'00179df8-84f3-5ed8-9ea6-469a85bf94aa'}
    catalog=dict(output_end='2026-10-07',target_count=17758,catalog_sha256='a'*64,us_prices=[member])
    original=b'{"bars":{}}'
    capture=make_capture(source='alpaca',kind='alpaca_bars',entity_id=member['entity_id'],
        captured_at=now,content=original,metadata={})
    def source(state,*args,**kwargs):
        state['turns']=state.get('turns',0)+1
        return [(capture,original if state['turns']==1 else b'tampered')]
    monkeypatch.setattr(acquisition,'_alpaca_step',source)
    state,captures=acquisition.acquire_tick(catalog,None,{'alpaca':object()},now=now,max_steps=2)
    assert captures==[(capture,original)]
    assert state['sources']['alpaca']['turns']==1
    assert state['provider_status']['alpaca']['status']=='deferred'
    assert state['provider_status']['alpaca']['error_type']=='ValueError'


def test_mutable_dart_list_page_boundary_reuses_exact_cached_filing_once(acquisition,monkeypatch):
    from datetime import date
    from backtest_data.offline.contracts import canonical_bytes,make_capture
    from backtest_data.offline.archives import build_bundles
    now=datetime(2026,11,20,tzinfo=timezone.utc)
    member={'entity_id':'00179df8-84f3-5ed8-9ea6-469a85bf94aa','identifier':'DART:00000001'}
    catalog=dict(output_end='2026-10-07',target_count=17758,catalog_sha256='a'*64,dart_issuers=[member])
    state=acquisition.new_state(catalog)
    state['sources']['opendart'].update(watermark='2026-11-18',cycle=dict(
        start='2026-11-19',end='2026-11-19',offset=0,page_no=1,pending=[]))
    monkeypatch.setattr(acquisition,'ready_day',lambda *args:date(2026,11,19))
    def filing(receipt):return dict(corp_code='00000001',rcept_no=receipt,report_nm='분기보고서')
    a,b='20261119000001','20261119000002'
    class Client:
        global_dart=True
        def dart_get(self,endpoint,params):
            assert endpoint=='list.json'
            return None,{'list':[filing(a)] if params['page_no']==1 else [filing(a),filing(b)],'total_page':2}
    fetched=[]
    def extract(item,client):
        receipt=item['filing']['rcept_no'];fetched.append(receipt)
        body=canonical_bytes(dict(version=1,format='offline_extraction_v1',source_content_hash='b'*64,result={}))
        capture=make_capture(source='opendart',kind='dart_extraction',entity_id=member['entity_id'],
            captured_at=now,content=body,metadata={'accession':receipt,'instance_key':'0'})
        return [(capture,body)]
    monkeypatch.setattr(acquisition,'_dart_filing',extract)
    state,captures=acquisition.acquire_tick(catalog,state,{'opendart':Client()},now=now,max_steps=10)
    assert fetched==[a,b]
    assert [c['metadata']['accession'] for c,_ in captures]==[a,b]
    assert state['reused_capture_observations']==1
    assert state['sources']['opendart']['watermark']=='2026-11-19'
    assert len(build_bundles(captures))==1
