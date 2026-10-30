import base64
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys

import pytest


def module(monkeypatch):
    directory = Path(__file__).resolve().parents[2] / 'scripts'
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location('maintenance_test', directory / 'offline_workflow_maintenance.py')
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


@pytest.mark.parametrize('accepted_attempt', [1, 4])
def test_ambiguous_health_write_reads_back_exact_bytes(monkeypatch, accepted_attempt):
    m = module(monkeypatch)
    current = None
    writes = 0
    def api(endpoint, *, method='GET', body=None):
        nonlocal current, writes
        if method == 'GET':
            if '/commits/' in endpoint: return {'sha': 'c' * 40}
            if current is None: raise m.GitHubError(404)
            return current
        writes += 1
        if writes == accepted_attempt:
            current = {'sha': 'a' * 40, 'content': body['content']}
        raise m.GitHubError(502, 'repository_write')
    result = m.publish_health(api, 'repos/a/b', 'main', {'observed_by_run_id': 101, 'run_attempt': 1}, sleep=lambda _: None)
    assert result == 'c' * 40
    assert writes == accepted_attempt


def test_health_authentication_failure_does_not_repeat_write(monkeypatch):
    m = module(monkeypatch)
    writes = []
    def api(endpoint, *, method='GET', body=None):
        if method == 'GET': raise m.GitHubError(404)
        writes.append(body)
        raise m.GitHubError(403, 'repository_write')
    with pytest.raises(m.GitHubError):
        m.publish_health(api, 'repos/a/b', 'main', {'observed_by_run_id': 101, 'run_attempt': 1}, sleep=lambda _: None)
    assert len(writes) == 1


def test_health_conflict_refreshes_sha_and_keeps_unrelated_files(monkeypatch):
    m = module(monkeypatch)
    reads, writes = [], []
    def api(endpoint, *, method='GET', body=None):
        if method == 'GET':
            reads.append(endpoint)
            return {'sha': ('a' if len(reads) == 1 else 'b') * 40, 'content': base64.b64encode(b'{}').decode()}
        writes.append((endpoint, body))
        if len(writes) == 1: raise m.GitHubError(409)
        return {'commit': {'sha': 'c' * 40}}
    assert m.publish_health(api, 'repos/a/b', 'main', {'observed_by_run_id': 101, 'run_attempt': 1}, sleep=lambda _: None) == 'c' * 40
    assert writes[0][1]['sha'] == 'a' * 40 and writes[1][1]['sha'] == 'b' * 40
    assert {endpoint for endpoint, _ in writes} == {'repos/a/b/contents/data/offline/collection-health.json'}
