import importlib
import json

import pytest

from backtest_data.ingestion.http import ProviderError


def module():
    return importlib.import_module('backtest_data.fundamentals.completion_metadata')


def test_source_block_classification_preserves_missing_document_as_a_local_gap():
    m = module()
    assert m.provider_failure(ProviderError('sec', '403')) == ('sec_source_403', True)
    assert m.provider_failure(ProviderError('sec', 'requires_identifying_user_agent'))[1]
    assert m.provider_failure(ProviderError('sec', 'transport_failed', retryable=True))[1]
    assert m.provider_failure(ProviderError('sec', '404')) == ('sec_source_404', False)
    assert m.provider_failure(ProviderError('sec', 'response_size_exceeded'))[1] is False


def test_blocked_metadata_checkpoint_stays_quiet_until_an_explicit_source_retry(tmp_path, monkeypatch):
    m = module()
    requests = []
    def blocked(database, **request):
        requests.append(request)
        return m.MetadataSummary(processed=1, unavailable=1, source_blocked=True,
            next_after_document_id='blocked', reasons={'sec_source_403': 1})
    monkeypatch.setattr(m, 'supplement_sec_metadata_batch', blocked)
    path = tmp_path / 'source.json'
    first = m.run_sec_metadata_worker(object(), sec=object(), checkpoint_path=path)
    second = m.run_sec_metadata_worker(object(), sec=object(), checkpoint_path=path)
    assert first['status'] == second['status'] == 'blocked' and len(requests) == 1
    assert json.loads(path.read_text())['reason'] == 'sec_source_403'
    m.run_sec_metadata_worker(object(), sec=object(), checkpoint_path=path, retry_source_block=True)
    assert len(requests) == 2 and requests[-1]['retry_source_failures'] is True


def test_metadata_main_redacts_database_failure(tmp_path, monkeypatch, capsys):
    m = module()
    from sqlalchemy.exc import OperationalError
    from types import SimpleNamespace
    monkeypatch.setattr(m.Settings, 'from_env', lambda: SimpleNamespace(database_url='redacted'))
    def fail(url):
        raise OperationalError('private statement', {'secret': 'do-not-output'}, Exception('private detail'))
    monkeypatch.setattr(m, 'make_engine', fail)
    assert m.main(['--checkpoint', str(tmp_path / 'error.json')]) == 2
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result['reason'] == 'OperationalError' and result['details_redacted'] is True
    assert 'private' not in output and 'do-not-output' not in output
