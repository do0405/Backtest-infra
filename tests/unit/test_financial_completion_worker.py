import importlib
import json

import pytest


def module():
    try:
        return importlib.import_module('backtest_data.fundamentals.completion_worker')
    except ModuleNotFoundError:
        pytest.fail('financial completion worker is missing')


def test_worker_covers_every_document_page_and_checkpoints_after_committed_batches(tmp_path):
    from backtest_data.fundamentals.completion_storage import CompletionSummary
    m = module()
    requests = []
    summaries = iter((CompletionSummary(documents=1, created=2, audited=1, next_after_document_id='a'),
        CompletionSummary(documents=1, created=0, audited=1, next_after_document_id='b',
                          reasons={'completion_source_fact_limit': 1}), CompletionSummary(), CompletionSummary()))
    def materializer(database, **request):
        requests.append(request)
        return next(summaries)
    checkpoint = tmp_path / 'full.json'
    result = m.run_completion_worker(object(), checkpoint_path=checkpoint, batch_size=1,
        max_source_facts=100, materializer=materializer)
    assert [request['after_document_id'] for request in requests] == ['', 'a', 'b', '']
    assert result['status'] == 'complete_with_deferred_documents'
    assert result['documents'] == 2 and result['created'] == 2
    assert result['deferred_document_ids'] == ['b']
    assert json.loads(checkpoint.read_text()) == result


def test_worker_resumes_checkpoint_and_a_failed_batch_never_advances_cursor(tmp_path):
    from backtest_data.fundamentals.completion_storage import CompletionSummary
    m = module()
    checkpoint = tmp_path / 'resume.json'
    first = m.run_completion_worker(object(), checkpoint_path=checkpoint, max_batches=1,
        materializer=lambda database, **kwargs: CompletionSummary(documents=1, created=1,
            audited=1, next_after_document_id='saved'))
    assert first['status'] == 'running'
    def fail(database, **request):
        assert request['after_document_id'] == 'saved'
        raise ValueError('deliberate_batch_failure')
    with pytest.raises(ValueError, match='deliberate_batch_failure'):
        m.run_completion_worker(object(), checkpoint_path=checkpoint, materializer=fail)
    saved = json.loads(checkpoint.read_text())
    assert saved['after_document_id'] == 'saved' and saved['documents'] == 1


def test_complete_checkpoint_does_not_skip_later_collected_documents(tmp_path):
    from backtest_data.fundamentals.completion_storage import CompletionSummary
    m = module()
    checkpoint = tmp_path / 'later.json'
    m.run_completion_worker(object(), checkpoint_path=checkpoint,
        materializer=lambda database, **kwargs: CompletionSummary())
    requests = []
    def later(database, **request):
        requests.append(request)
        return CompletionSummary(documents=1, created=1, next_after_document_id='new')
    resumed = m.run_completion_worker(object(), checkpoint_path=checkpoint, max_batches=1, materializer=later)
    assert len(requests) == 1 and resumed['documents'] == 1
    assert resumed['status'] == 'running'


def test_worker_main_redacts_database_errors_and_exits_with_recoverable_receipt(tmp_path, monkeypatch, capsys):
    m = module()
    from sqlalchemy.exc import OperationalError
    from types import SimpleNamespace
    monkeypatch.setattr(m.Settings, 'from_env', lambda: SimpleNamespace(database_url='redacted'))
    def fail(url):
        raise OperationalError('unsafe statement', {'secret': 'do-not-output'},
                               Exception('unsafe connection detail'), connection_invalidated=True)
    monkeypatch.setattr(m, 'make_engine', fail)
    assert m.main(['--checkpoint', str(tmp_path / 'error.json')]) == 2
    output = capsys.readouterr().out
    receipt = json.loads(output)
    assert receipt['status'] == 'blocked' and receipt['reason'] == 'OperationalError'
    assert receipt['details_redacted'] is True
    assert 'unsafe' not in output and 'do-not-output' not in output


def test_worker_removes_resolved_deferred_ids_when_resumed_with_a_larger_budget(tmp_path):
    from backtest_data.fundamentals.completion_storage import CompletionSummary
    m = module()
    checkpoint = tmp_path / 'deferred.json'
    initial = iter((CompletionSummary(documents=1, next_after_document_id='large',
        reasons={'completion_source_fact_limit': 1}), CompletionSummary(), CompletionSummary()))
    m.run_completion_worker(object(), checkpoint_path=checkpoint, max_source_facts=1,
        materializer=lambda database, **request: next(initial))
    resumed = iter((CompletionSummary(documents=1, scanned=2, created=2, next_after_document_id='large'),
        CompletionSummary(), CompletionSummary()))
    result = m.run_completion_worker(object(), checkpoint_path=checkpoint, max_source_facts=2,
        materializer=lambda database, **request: next(resumed))
    assert result['status'] == 'complete' and result['deferred_document_ids'] == []
