from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock


def test_owned_rate_restore_and_verified_admission_precede_source_http(tmp_path, monkeypatch):
    from backtest_data.remote import worker
    from backtest_data.ingestion import quota
    events = []; checkpoints = []
    manifest = SimpleNamespace(id='original',members=[None] * 17758,
        scope={'output_start': '2016-01-01', 'output_end': '2026-10-07'})
    session = Mock()
    session.scalar.return_value = None
    session.scalars.return_value = []
    session.get.return_value = manifest
    @contextmanager
    def transaction(engine):
        yield session
    monkeypatch.setattr(worker, 'transaction', transaction)
    monkeypatch.setattr(worker, 'tables', lambda: {})
    monkeypatch.setattr(worker, 'validate_ownership', lambda value: [{'source': 'krx', 'domain': 'prices'}])
    monkeypatch.setattr(quota, 'restore_rate_state', lambda *args, **kwargs: events.append('restore'))
    monkeypatch.setattr(quota, 'snapshot_rate_state', lambda *args, **kwargs: events.append('snapshot') or {'version': 1})
    monkeypatch.setattr(worker, 'export_deltas', lambda *args, **kwargs: checkpoints.append(kwargs['checkpoint']) or [tmp_path])
    monkeypatch.setattr(worker, 'FolderTransport', lambda *args, **kwargs: SimpleNamespace(publish_group=lambda paths: {}))
    def admission(session):
        events.append('grant')
    def handler(*args, **kwargs):
        events.append('source_http'); return {}
    worker.run_cycle(None, tmp_path, ownership={'owner_id': 'krx-test', 'manifest_id': 'original'},
                     planned_jobs=[], incoming_bundles=[], output_root=tmp_path,
                     transport_root=tmp_path, stream_id='krx-test', sequence=1,
                     checkpoint={'provider_rate_state': {'version': 1}}, budget_seconds=300,
                     source_handlers=handler, rate_admission=admission)
    assert events == ['restore', 'grant', 'source_http', 'snapshot']
    assert checkpoints[0]['provider_rate_state'] == {'version': 1}
    assert checkpoints[0]['coverage_complete'] is False


def test_no_allocation_preserves_handler_checkpoint_without_source_calls():
    from backtest_data.remote.quota import deferred_handler
    previous = {'krx_quotes': {'planned_through': '2020-01-31'}}
    result = deferred_handler(None, None, checkpoint={'source_handler_state': previous})
    assert result['source_handler_state'] == previous
    assert result['source_handler_outcomes'][0]['collection']['blocked_sources'] == {'krx': 'rate_budget_deferred'}
