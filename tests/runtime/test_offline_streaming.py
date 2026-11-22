"""Capacity is per immutable publication, provider admission is per run."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def refresh(monkeypatch):
    root = os.environ.get('OFFLINE_RUNTIME_SOURCE')
    if not root:
        pytest.skip('OFFLINE_RUNTIME_SOURCE required')
    root = Path(root)
    monkeypatch.syspath_prepend(str(root/'src'))
    monkeypatch.syspath_prepend(str(root/'scripts'))
    spec = importlib.util.spec_from_file_location('offline_streaming_test', root/'scripts/offline_refresh.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configured(refresh, monkeypatch, *, acknowledge=True):
    calls=[]
    budget=SimpleNamespace(used={'alpaca':0},remaining={'alpaca':4})
    def collect(catalog,state,clients,**kwargs):
        calls.append(('collect',deepcopy(state)))
        budget.remaining['alpaca']-=1;budget.used['alpaca']+=1
        offset=(state or {}).get('offset',0)+1
        status='manifest_deferred' if offset==1 else 'captured'
        return {'offset':offset,'provider_status':{'alpaca':{'status':status}}},[({'capture_id':str(offset)},b'raw')]
    def publish(bundles,state):
        calls.append(('publish',deepcopy(state)))
        return SimpleNamespace(tag='offline-core-forward-v1-'+str(state['offset']).zfill(9))
    monkeypatch.setattr(refresh,'acquire_tick',collect)
    monkeypatch.setattr(refresh,'build_bundles',lambda values:values)
    monkeypatch.setattr(refresh,'acknowledge',lambda *args:acknowledge)
    monkeypatch.setattr(refresh,'remove_local',lambda *args:calls.append(('clean',)))
    transport=SimpleNamespace(publish=publish,pending_publication=({},{}))
    return calls,budget,transport


def test_capacity_flush_continues_from_committed_cursor_and_reuses_run_budget(refresh,monkeypatch):
    calls,budget,transport=configured(refresh,monkeypatch)
    refresh.collect_publications({},None,{},budget,transport,deadline=float('inf'),progress={},
        on_commit=lambda commit,state:calls.append(('indexed',state['offset'])))
    assert [x[0] for x in calls]==['collect','publish','indexed','clean']*2
    assert calls[4][1]['offset']==1
    assert [x[1]['used_calls']['alpaca'] for x in calls if x[0]=='publish']==[1,2]
    assert budget.remaining['alpaca']==2


def test_unacknowledged_publication_stops_before_more_provider_requests(refresh,monkeypatch):
    calls,budget,transport=configured(refresh,monkeypatch,acknowledge=False)
    refresh.collect_publications({},None,{},budget,transport,deadline=float('inf'),progress={},on_commit=lambda *a:None)
    assert sum(x[0]=='collect' for x in calls)==1
    assert not any(x[0]=='clean' for x in calls)


def test_exhausted_provider_is_not_recollected_after_capacity_flush(refresh,monkeypatch):
    calls,budget,transport=configured(refresh,monkeypatch)
    budget.remaining['alpaca']=1
    refresh.collect_publications({},None,{},budget,transport,deadline=float('inf'),progress={},on_commit=lambda *a:None)
    assert sum(x[0]=='collect' for x in calls)==1


def test_index_failure_preserves_committed_state_and_does_not_collect_again(refresh,monkeypatch):
    calls,budget,transport=configured(refresh,monkeypatch)
    progress={}
    def failed(commit,state):
        progress.update(publication_state='committed',phase='source_index')
        raise RuntimeError('readback failed')
    with pytest.raises(RuntimeError,match='readback failed'):
        refresh.collect_publications({},None,{},budget,transport,deadline=float('inf'),progress=progress,on_commit=failed)
    assert progress['publication_state']=='committed'
    assert sum(x[0]=='collect' for x in calls)==1


@pytest.mark.parametrize(('seconds','credits'),[(0,0),(95,9),(5400,540),(20100,2010),(50000,2010)])
def test_alpaca_admission_uses_existing_rate_and_remaining_job_time(refresh,seconds,credits):
    assert refresh.alpaca_run_credits(seconds)==credits


@pytest.mark.parametrize('seconds',[-1,float('inf'),float('nan'),True,'600'])
def test_invalid_acquisition_window_cannot_authorize_requests(refresh,seconds):
    with pytest.raises(ValueError):refresh.alpaca_run_credits(seconds)


def test_streamed_chunks_keep_real_archives_hash_chain_and_recovery_cleanup(refresh,monkeypatch,tmp_path):
    from test_offline_outbox import Remote,configured as configure_transport
    from backtest_data.offline.contracts import make_capture
    remote=Remote();transport=configure_transport(tmp_path,monkeypatch,remote)
    budget=SimpleNamespace(used={'alpaca':0},remaining={'alpaca':4})
    caches=[];commits=[]
    def acquire(catalog,state,clients,**kwargs):
        caches.append(kwargs['dart_cache'])
        offset=(state or {}).get('offset',0)+1
        budget.used['alpaca']+=1;budget.remaining['alpaca']-=1
        content=('{}.{}'.format('original source',offset)).encode()
        capture=make_capture(source='alpaca',kind='alpaca_bars',
            entity_id='00179df8-84f3-5ed8-9ea6-469a85bf94aa',
            captured_at=datetime(2026,11,23,tzinfo=timezone.utc),content=content,metadata={})
        return {'catalog_sha256':'a'*64,'offset':offset,'provider_status':{
            'alpaca':{'status':'storage_deferred' if offset==1 else 'captured'}}},[(capture,content)]
    monkeypatch.setattr(refresh,'acquire_tick',acquire)
    refresh.collect_publications({},None,{},budget,transport,deadline=float('inf'),progress={},
        on_commit=lambda commit,state:commits.append(commit))
    assert [c.sequence for c in commits]==[1,2]
    assert commits[1].manifest['previous_commit_sha256']==commits[0].sha256
    assert all(len(c.manifest['captures'])==1 for c in commits)
    assert caches[0] is caches[1]
    assert len(remote.releases)==2 and all(not r['draft'] for r in remote.releases.values())
