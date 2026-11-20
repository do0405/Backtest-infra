from types import SimpleNamespace
import subprocess

import pytest
from backtest_data.remote import releases


@pytest.mark.parametrize('stderr', [b'temporary failure (HTTP 502)', b'API rate limit exceeded (HTTP 403)', b'connection reset by peer'])
def test_transient_inventory_reads_retry_without_leaking_error_body(monkeypatch, stderr):
    calls = [];waits=[]
    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1 if len(calls)==1 else 0, stdout=b'[]', stderr=stderr)
    monkeypatch.setattr(releases.subprocess, 'run', run)
    monkeypatch.setattr(releases.time, 'sleep', waits.append)
    assert releases.github('gh', ['api', '--paginate', '--slurp', 'repos/a/b/releases']) == b'[]'
    assert len(calls)==2 and waits==[2]


def test_exhausted_reads_keep_safe_status_and_authentication_does_not_retry(monkeypatch):
    calls=[]
    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1, stdout=b'', stderr=b'sensitive response (HTTP 401)')
    monkeypatch.setattr(releases.subprocess, 'run', run)
    with pytest.raises(releases.GitHubAccessError) as caught:
        releases.github('gh', ['api','repos/a/b/releases'])
    assert caught.value.safe_details()=={'http_status':401,'operation':'runtime_read'}
    assert len(calls)==1 and 'sensitive' not in str(caught.value)


@pytest.mark.parametrize('arguments', [
    ['release','create','reserved-tag'],
    ['api','repos/a/b/releases','--input','-'],
    ['api','repos/a/b/releases','-f','tag_name=reserved-tag'],
    ['api','repos/a/b/releases','--method=POST'],
    ['api','repos/a/b/releases','-XPOST'],
])
def test_ambiguous_writes_are_not_blindly_replayed(monkeypatch, arguments):
    calls=[]
    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1, stdout=b'', stderr=b'unknown write result (HTTP 502)')
    monkeypatch.setattr(releases.subprocess, 'run', run)
    with pytest.raises(releases.GitHubOperationError) as caught:
        releases.github('gh', arguments)
    assert caught.value.retryable and len(calls)==1
    assert caught.value.safe_details()['operation']=='runtime_write'


def test_read_timeout_is_typed_and_bounded(monkeypatch):
    calls=[]
    def run(*args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired(args, 120)
    monkeypatch.setattr(releases.subprocess, 'run', run)
    monkeypatch.setattr(releases.time, 'sleep', lambda delay:None)
    with pytest.raises(releases.GitHubOperationError) as caught:
        releases.github('gh',['api','repos/a/b/releases'])
    assert caught.value.retryable and len(calls)==4
    assert caught.value.safe_details()=={'http_status':None,'operation':'runtime_read'}
