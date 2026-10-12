from copy import deepcopy
import importlib
from datetime import datetime, timezone
import json

import pytest


POLICY = {'version': 1, 'manifest_id': 'original-manifest',
          'output_start': '2016-01-01', 'output_end': '2026-10-07',
          'forward_stream_id': 'core-forward-v1', 'catalog_sha256': 'a' * 64,
          'forward_sources': ['alpaca', 'sec', 'opendart']}


def choose(role, policy=None, previous=None):
    module = importlib.import_module('backtest_data.ops.source_cadence')
    return module.local_refresh_sources(role, ownership=policy,
        manifest_id='original-manifest', previous=previous)


def test_standalone_defaults_and_product_authority_remain_local():
    assert choose('prices') == ('kis', 'kis_overseas')
    assert choose('alpaca_prices') == ('alpaca',)
    assert choose('fundamentals') == ('opendart', 'sec')
    assert choose('prices', POLICY) == ('kis', 'kis_overseas')
    assert choose('alpaca_prices', POLICY) == ()
    assert choose('fundamentals', POLICY) == ()
    with pytest.raises(ValueError):
        choose('supplier_prices', POLICY)


@pytest.mark.parametrize('field,value', [('manifest_id', 'other'), ('output_end', '2026-10-08'),
    ('forward_sources', ['kis']), ('forward_stream_id', 'unknown'), ('catalog_sha256', 'bad')])
def test_bad_ownership_binding_cannot_silently_transfer_a_source(field, value):
    policy = deepcopy(POLICY); policy[field] = value
    with pytest.raises(ValueError, match='ownership'):
        choose('alpaca_prices', policy)


def test_frozen_incomplete_planning_keeps_exact_sources_until_it_drains():
    previous = {'status': 'planning', 'sources': ['alpaca'], 'cursor': 250, 'plan_hash': 'frozen'}
    before = deepcopy(previous)
    assert choose('alpaca_prices', POLICY, previous) == ('alpaca',)
    assert previous == before
    assert choose('alpaca_prices', POLICY, {**previous, 'status': 'ready'}) == ()


def test_remote_owned_completed_pass_is_normal_and_never_launches(tmp_path):
    from backtest_data.ops.continuation_manager import ContinuationManager
    root = tmp_path / 'artifacts/continuous/runs/test'; root.mkdir(parents=True)
    entry = {'runtime': str(root / 'old.runtime.json'), 'run_id': 'old', 'pid': 10}
    (tmp_path / 'artifacts/kr-full-workers.json').write_text(json.dumps(
        {'results': {}, 'auxiliary': {'alpaca_prices': entry}}))
    class Host:
        calls = 0
        def plan_completed_refresh(self, *args, **kwargs):
            return {'status': 'remote_owned', 'checkpoint': None, 'coverage_complete': False}
        def launch(self, *args):
            self.calls += 1
            raise AssertionError('source must not launch')
    host = Host()
    manager = ContinuationManager(tmp_path, root, host, roles=['alpaca_prices'],
        clock=lambda: datetime(2026, 10, 12, tzinfo=timezone.utc).timestamp(),
        refresh_manifest_id='original-manifest')
    manager.state['roles']['alpaca_prices'] = {'entry_runtime': entry['runtime'], 'status': 'complete'}
    for _ in range(3): manager.tick()
    item = manager.state['roles']['alpaca_prices']
    assert item['status'] == 'waiting_for_remote_refresh'
    assert not item['requires_user'] and host.calls == 0
    assert item['coverage_complete'] is False


@pytest.mark.parametrize('pending,status', [(False, 'remote_owned'), (True, 'ready'), (None, 'queue_state_unconfirmed')])
def test_host_remote_fence_preserves_pending_admission_without_building_new_plan(tmp_path, pending, status):
    from backtest_data.ops.continuation_host import WindowsHost
    (tmp_path / 'artifacts').mkdir()
    (tmp_path / 'artifacts/source-ownership.json').write_text(json.dumps(POLICY))
    host = WindowsHost(tmp_path, 'unused', 'unused')
    host.runnable = lambda role: pending
    result = host._plan_completed_refresh_locked('alpaca_prices', 'original-manifest', None,
        datetime(2026, 10, 12, tzinfo=timezone.utc))
    assert result['status'] == status and host.engine is None


def test_registration_barrier_requires_exact_controller_identity(tmp_path):
    from backtest_data.ops.continuation_manager import registration_admitted
    path = tmp_path / 'registration-ready.json'
    assert not registration_admitted(path, run_id='current', manager_pid=123)
    path.write_text(json.dumps({'run_id': 'old', 'manager_pid': 123}))
    with pytest.raises(ValueError, match='registration'):
        registration_admitted(path, run_id='current', manager_pid=123)
    path.write_text(json.dumps({'run_id': 'current', 'manager_pid': 456}))
    with pytest.raises(ValueError, match='registration'):
        registration_admitted(path, run_id='current', manager_pid=123)
    path.write_text(json.dumps({'run_id': 'current', 'manager_pid': 123}))
    assert registration_admitted(path, run_id='current', manager_pid=123)
