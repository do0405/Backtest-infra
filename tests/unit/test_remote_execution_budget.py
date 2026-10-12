"""Offline clocks prove that disposable runner setup cannot restart acquisition."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import Mock

import pytest

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)
SCOPE = {'manifest_id': 'original', 'target_count': 17758,
         'target_hash': 'a' * 64, 'market_target_counts': {'KR': 4507, 'US': 13251},
         'output_start': '2016-01-01', 'output_end': '2026-10-07'}
SLOT = {'source': 'krx', 'domain': 'prices', 'market': 'kr', 'owner': 'remote',
        'local_enabled': False, 'account_id': 'original-account'}


class MemoryPath:
    def __init__(self, value): self.value = str(value)
    def resolve(self): return self
    def __truediv__(self, value): return MemoryPath(self.value + '/' + str(value))
    def mkdir(self, **kwargs): pass


class CycleCaptured(Exception): pass


def release_probe(monkeypatch, *, catalog_seconds, budget=6000, allowance=True):
    path = Path(__file__).resolve().parents[2] / 'scripts/remote_release_cycle.py'
    spec = importlib.util.spec_from_file_location('remote_release_budget_test', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    clock = [100.0]; seen = {}
    configuration = {'owner_id': 'krx-test', 'handoff_id': 'same-generation',
        'manifest_id': 'original', 'bootstrap': {'tag': 'original-bootstrap',
        'sha256': 'a' * 64, 'state_sha256': 'a' * 64}}
    checkpoint = {'handoff_id': 'same-generation', 'task_state': [],
                  'raw_identity_index': {'version': 1, 'rows': []}}
    allocation = {'credits_by_day': {'2026-10-12': 608},
                  'expires_at': (NOW + timedelta(hours=2)).isoformat()} if allowance is True else allowance
    @contextmanager
    def transaction(_): yield N()
    def import_catalog(*args, **kwargs): clock[0] += catalog_seconds
    def cycle(*args, **kwargs): seen.update(kwargs); raise CycleCaptured
    def reserve(*args, **kwargs):
        seen['allocation_budget_seconds'] = kwargs['budget_seconds']
        return allocation
    replacements = dict(Path=MemoryPath, validate_ownership=lambda _: [SLOT],
        github=lambda *a, **k: b'{}', download_asset=lambda *a, **k: MemoryPath('download'),
        digest=lambda _: 'a' * 64, restore=lambda *a: None, read_state=lambda _: checkpoint,
        inventory=lambda *a: [], validate_raw_identity_index=lambda _: None,
        reserve_hosted_allowance=reserve,
        make_engine=lambda _: N(dispose=lambda: None), transaction=transaction,
        install_quota_controls=lambda _: None, upsert_quota_policy=lambda *a, **k: None,
        FolderTransport=lambda _: N(committed_bundles=lambda: ['catalog']),
        import_bundle=import_catalog, run_cycle=cycle,
        utcnow=lambda: NOW + timedelta(seconds=clock[0] - 100))
    for name, value in replacements.items(): monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(module.shutil, 'which', lambda _: 'fake-gh')
    from backtest_data.remote import worker
    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.sys, 'argv', ['probe', '--work', 'in-memory', '--budget-seconds', str(budget)])
    for name, value in {'REPOSITORY': 'owner/repo', 'REMOTE_OWNERSHIP': json.dumps(configuration),
        'GITHUB_RUN_ID': '1', 'GITHUB_SHA': 'a' * 40, 'BACKTEST_DATABASE_URL': 'never-connect'}.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(CycleCaptured): module.main()
    return module, seen


@pytest.mark.parametrize('catalog_seconds,budget,expected', [(0, 6000, 6000), (900, 6000, 5100), (900, 300, 300), (6100, 6000, 0)])
def test_catalog_elapsed_reduces_only_the_remaining_hosted_envelope(monkeypatch, catalog_seconds, budget, expected):
    _, seen = release_probe(monkeypatch, catalog_seconds=catalog_seconds, budget=budget)
    assert seen['budget_seconds'] == expected
    assert seen['acquisition_deadline'] == 6100


@pytest.mark.parametrize('allowance', [None, {'credits_by_day': {'2026-10-12': 608}}])
def test_no_usable_allowance_keeps_deferred_export_without_http(monkeypatch, allowance):
    module, seen = release_probe(monkeypatch, catalog_seconds=900, allowance=allowance)
    assert seen['source_handlers'] is module.deferred_handler


def test_release_phase_output_has_only_safe_small_clock_records(monkeypatch, capsys):
    release_probe(monkeypatch, catalog_seconds=900)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {'quota_committed', 'catalog_restored'} <= {row['phase'] for row in records}
    assert all(set(row) <= {'status', 'phase', 'elapsed_seconds', 'remaining_job_seconds'} for row in records)
    assert all(len(json.dumps(row)) < 250 for row in records)


def test_publication_tail_does_not_extend_source_clock_or_overreserve_requests(monkeypatch):
    module, seen = release_probe(monkeypatch, catalog_seconds=900)
    assert module.PUBLICATION_TAIL_SECONDS == module.EXPECTED_COMPLETION_SECONDS == 7200
    assert seen['acquisition_deadline'] == 6100
    assert seen['allocation_budget_seconds'] == 2560
    assert seen['source_handlers'].keywords['krx_batch_limits'] == {
        'max_requests': 256, 'max_new_rows': 500000, 'max_raw_bytes': 128 * 1024 ** 2}
    import yaml
    workflow = yaml.safe_load((Path(__file__).resolve().parents[2]/'.github/workflows/remote-data-update.yml').read_text())
    assert workflow['jobs']['update']['timeout-minutes'] * 60 == 7200 + module.PUBLICATION_TAIL_SECONDS


@pytest.mark.parametrize('budget', [6000, 0])
def test_expired_restoration_still_exports_exact_queue_raw_and_quota_state(monkeypatch, budget):
    from backtest_data.remote import worker
    from backtest_data.ingestion import quota
    clock = [0.0]; events = []; exported = []
    manifest = N(id='original', members=[None] * 17758, scope=SCOPE)
    job = N(id='original-job', identity_hash='b' * 64, source='krx', domain='market_quotes',
        entity_id='kospi:2016-01-01', request={'manifest_id': 'original', 'service': 'kospi',
        'session_date': '2016-01-01'}, priority=10, status='retry', attempts=4,
        checkpoint={'inserted': Decimal('123.00'), 'request_anchor': NOW.isoformat()})
    prior = {'source_handler_state': {'krx_quotes': {'scope': SCOPE, 'planned_through': '2026-10-07'}},
        'raw_identity_index': {'version': 1, 'rows': [{'id': 'original-raw', 'created_at': NOW}]},
        'provider_rate_state': {'version': 1, 'unchanged_credit_charge': 608}, 'task_state': [job]}
    session = Mock(); session.get.return_value = manifest; session.scalars.return_value = [job]
    @contextmanager
    def transaction(_): yield session
    def restore_tasks(*args, **kwargs):
        assert args[1] is prior['task_state']; clock[0] = 1500; events.append('tasks_restored')
    monkeypatch.setattr(worker, 'transaction', transaction)
    monkeypatch.setattr(worker, 'tables', lambda: {})
    monkeypatch.setattr(worker, 'validate_ownership', lambda _: [SLOT])
    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(worker, 'restore_tasks', restore_tasks)
    monkeypatch.setattr(worker, 'restore_raw_identities', lambda *a, **k: events.append('raw_restored'))
    monkeypatch.setattr(worker, 'snapshot_raw_identities', lambda *a, **k: prior['raw_identity_index'])
    monkeypatch.setattr(quota, 'restore_rate_state', lambda *a, **k: events.append('rate_restored'))
    monkeypatch.setattr(quota, 'snapshot_rate_state', lambda *a, **k: prior['provider_rate_state'])
    monkeypatch.setattr(worker, 'export_deltas', lambda *a, **k: exported.append(k['checkpoint']) or ['bundle'])
    monkeypatch.setattr(worker, 'FolderTransport', lambda *a, **k: N(publish_group=lambda _, **kw: {'sequence': 12}))
    phases = []
    worker.run_cycle(None, 'raw', ownership={'owner_id': 'krx-test', 'manifest_id': 'original'},
        planned_jobs=[], incoming_bundles=[], output_root='out', transport_root='store',
        stream_id='krx-test', sequence=12, checkpoint=prior, budget_seconds=budget,
        acquisition_deadline=1000, source_handlers=lambda *a, **k: pytest.fail('HTTP after expiry'),
        rate_admission=lambda _: events.append('allocation_admitted'), phase_callback=phases.append)
    assert events == ['raw_restored', 'tasks_restored', 'rate_restored', 'allocation_admitted']
    saved = exported[0]
    assert saved['source_handler_state'] == prior['source_handler_state']
    assert saved['raw_identity_index'] == prior['raw_identity_index']
    assert saved['provider_rate_state'] == prior['provider_rate_state']
    assert saved['task_state'][0]['id'] == job.id and saved['task_state'][0]['attempts'] == 4
    assert saved['task_state'][0]['checkpoint'] == job.checkpoint
    assert saved['unfinished_task_ids'] == [job.identity_hash] and saved['coverage_complete'] is False
    assert 'checkpoint_export_started' in phases and 'checkpoint_export_completed' in phases


def test_handler_scope_restore_cannot_restart_its_execution_budget(monkeypatch):
    from backtest_data.remote import source_handlers as module
    clock = [0.0]; target = N(id='original', content_hash='a' * 64, scope=SCOPE,
        members=[{'market': 'KR'}] * 4507 + [{'market': 'US'}] * 13251)
    @contextmanager
    def transaction(_):
        clock[0] = 11
        yield N(get=lambda *a: target)
    monkeypatch.setattr(module, 'transaction', transaction)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module, 'krx_quote_step', lambda *a, **k: pytest.fail('HTTP after scope restore'))
    prior = {'krx_quotes': {'scope': SCOPE, 'planned_through': '2026-10-07'}}
    result = module.run_owned_handlers(None, 'raw', manifest_id='original', slots=[SLOT],
        owned_sources={'krx'}, checkpoint={'source_handler_state': prior}, budget_seconds=10,
        work_root='unused', as_of=NOW)
    assert result['source_handler_state'] == prior
    assert result['source_handler_outcomes'][0]['status'] == 'budget_deferred'


def test_krx_planning_expiry_keeps_its_committed_cursor_without_http(monkeypatch):
    from backtest_data.remote import source_handlers as module
    clock = [0.0]; target = N(id='original', content_hash='a' * 64, scope=SCOPE,
        members=[{'market': 'KR'}] * 4507 + [{'market': 'US'}] * 13251)
    @contextmanager
    def transaction(_):
        yield N(get=lambda *a: target, scalars=lambda _: N(yield_per=lambda _: []))
    def plan(*args, **kwargs): clock[0] = 11; return {'created_jobs': 310}
    monkeypatch.setattr(module, 'transaction', transaction)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module, 'plan_krx_quotes', plan)
    monkeypatch.setattr(module, 'run_krx_quote_collection', lambda *a, **k: pytest.fail('HTTP after planning'))
    prior = {'scope': SCOPE, 'planned_through': '2016-01-31'}
    state, outcome = module.krx_quote_step(None, 'raw', scope=SCOPE, previous=prior,
        budget_seconds=10, as_of=NOW)
    assert state['planned_through'] == '2016-03-02'
    assert outcome['collection']['status'] == 'budget_deferred'
    assert state['scope'] == prior['scope'] and state['coverage_complete'] is False


def collector_probe(monkeypatch, *, credential_seconds=0, acquire_seconds=0, lost=False):
    from backtest_data.prices import krx_quote_collection as module
    from backtest_data import providers
    from backtest_data.ingestion.jobs import JobLease, LeaseLost
    clock = [0.0]; seen = []; completions = []
    job = N(request={'service': 'kospi', 'session_date': '2026-10-07'},
            checkpoint={'request_anchor': NOW.isoformat(), 'inserted': Decimal('2.00')})
    @contextmanager
    def transaction(_): yield N(get=lambda *a: job)
    def credentials(**kwargs):
        clock[0] += credential_seconds
        return N(krx_api_key=True)
    def acquire(*args, **kwargs):
        if seen: return None
        clock[0] += acquire_seconds
        return JobLease('original-job', 'current-owner', 7)
    def complete(session, lease, now, checkpoint, *, status):
        assert lease == JobLease('original-job', 'current-owner', 7)
        if lost: raise LeaseLost('superseded')
        completions.append((lease, checkpoint, status))
    def daily(*args):
        seen.append(args)
        return N(), {}
    monkeypatch.setattr(module, 'transaction', transaction)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module, 'load_credentials', credentials)
    monkeypatch.setattr(module, 'ProviderHTTP', lambda _: N(close=lambda: None))
    monkeypatch.setattr(providers, 'KRXData', lambda *a: N(daily_quotes=daily))
    monkeypatch.setattr(module, 'acquire_job', acquire)
    monkeypatch.setattr(module, 'complete_job', complete)
    monkeypatch.setattr(module, 'store_krx_quotes', lambda *a, **k: {'response_valid': True})
    monkeypatch.setattr(module, '_queue_counts', lambda *a: {'retry': 1, 'pending': 9})
    return module, seen, completions, job


def test_credentials_do_not_restart_the_collector_clock(monkeypatch):
    module, seen, completions, _ = collector_probe(monkeypatch, credential_seconds=11)
    result = module.run_krx_quote_collection(None, 'raw', budget_seconds=10)
    assert seen == completions == []
    assert result['budget_exhausted'] is True and result['processed_this_run'] == 0


@pytest.mark.parametrize('lost', [False, True])
def test_slow_claim_never_starts_http_after_deadline_or_revives_a_lease(monkeypatch, lost):
    module, seen, completions, job = collector_probe(monkeypatch, acquire_seconds=11, lost=lost)
    result = module.run_krx_quote_collection(None, 'raw', budget_seconds=10)
    assert seen == []
    if lost:
        assert completions == [] and result['blocked_sources'] == {'krx': 'lease_lost'}
    else:
        assert completions[0][1] == job.checkpoint and completions[0][2] == 'retry'
        assert result['blocked_sources'] == {} and result['budget_exhausted'] is True


def test_collector_obeys_an_earlier_outer_deadline(monkeypatch):
    module, seen, completions, _ = collector_probe(monkeypatch, acquire_seconds=6)
    result = module.run_krx_quote_collection(None, 'raw', budget_seconds=10, deadline=5)
    assert seen == [] and completions[0][2] == 'retry'
    assert result['budget_exhausted'] is True
