"""Run against the extracted hash-pinned runtime with OFFLINE_RUNTIME_SOURCE."""
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

source = os.environ.get('OFFLINE_RUNTIME_SOURCE')
if not source:
    pytest.skip('OFFLINE_RUNTIME_SOURCE selects the separately deployed runtime', allow_module_level=True)
runtime = Path(source).resolve()
sys.path.insert(0, str(runtime/'src'))
sys.path.insert(0, str(runtime/'scripts'))
import offline_publication as publication
from backtest_data.offline import transport
from backtest_data.offline.archives import build_bundles
from backtest_data.offline.contracts import make_capture


class Remote:
    def __init__(self, *, ambiguous_create=False):
        self.release = None
        self.calls = []
        self.ambiguous_create = ambiguous_create

    def __call__(self, gh, endpoint, *, method='GET', body=None, file=None):
        self.calls.append((method, endpoint))
        if method == 'POST' and endpoint.endswith('/releases'):
            self.release = {'id': 123, 'tag_name': body['tag_name'], 'draft': True,
                            'prerelease': False, 'assets': []}
            if self.ambiguous_create:
                self.ambiguous_create = False
                raise publication.GitHubError(502, 'release_create')
        elif method == 'POST':
            raw = Path(file).read_bytes()
            self.release['assets'].append({'id': len(self.release['assets'])+1,
                'name': endpoint.split('name=')[1], 'state': 'uploaded', 'size': len(raw),
                'digest': 'sha256:' + sha256(raw).hexdigest()})
        elif method == 'PATCH': self.release['draft'] = False
        elif method != 'GET': raise AssertionError('unexpected remote mutation')
        return json.loads(json.dumps(self.release))


def prepared(tmp_path, monkeypatch, remote):
    monkeypatch.setattr(publication, 'github', remote)
    value = transport.GitHubCaptureTransport('a/b', workspace=tmp_path, catalog_sha256='a'*64)
    monkeypatch.setattr(value, 'last_committed', lambda: None)
    monkeypatch.setattr(value, '_inventory', lambda: [remote.release] if remote.release else [])
    raw = b'{"bars":[]}'
    capture = make_capture(source='alpaca', kind='alpaca_bars',
        entity_id='00179df8-84f3-5ed8-9ea6-469a85bf94aa',
        captured_at=datetime(2026, 10, 30, tzinfo=timezone.utc), content=raw, metadata={})
    return value, build_bundles([(capture, raw)]), {'catalog_sha256': 'a'*64}, capture


@pytest.mark.parametrize('ambiguous_create', [False, True])
def test_runtime_publishes_original_capture_and_reconciles_create(tmp_path, monkeypatch, ambiguous_create):
    remote = Remote(ambiguous_create=ambiguous_create)
    value, bundles, state, capture = prepared(tmp_path, monkeypatch, remote)
    commit = value.publish(bundles, state)
    assert commit.manifest['captures'] == [{'asset': 'captures-0001.zip', 'capture': capture}]
    assert commit.release['draft'] is False
    assert sum(method == 'POST' and url.endswith('/releases') for method, url in remote.calls) == 1
    assert all(method != 'DELETE' for method, _ in remote.calls)


def test_runtime_preserves_conflicting_pending_generation(tmp_path, monkeypatch):
    remote = Remote()
    value, bundles, state, capture = prepared(tmp_path, monkeypatch, remote)
    remote.release = {'id': 123, 'tag_name': 'offline-core-forward-v1-000000001',
        'draft': True, 'prerelease': False, 'assets': [{'id': 1, 'name': 'captures-0001.zip',
        'size': 5, 'state': 'uploaded', 'digest': 'sha256:' + 'b'*64}]}
    before = json.dumps(remote.release)
    with pytest.raises(ValueError, match='preserved'):
        value.publish(bundles, state)
    assert json.dumps(remote.release) == before
    assert all(method == 'GET' for method, _ in remote.calls)


def test_runtime_index_recovers_an_accepted_write_with_lost_response(tmp_path, monkeypatch):
    import base64
    spec = importlib.util.spec_from_file_location('offline_refresh_test', runtime/'scripts/offline_refresh.py')
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    current, writes = None, []
    def api(gh, endpoint, *, method='GET', body=None):
        nonlocal current
        if method == 'GET':
            if current is None: raise publication.GitHubError(404)
            return current
        writes.append(body)
        current = {'sha': 'a'*40, 'content': body['content']}
        raise publication.GitHubError(502, 'repository_write')
    monkeypatch.setattr(publication, 'github', api)
    commit = transport.ReleaseCommit('offline-core-forward-v1-000000101', 101, 'b'*64,
        {'stream_id': 'core-forward-v1', 'catalog_sha256': 'c'*64}, {})
    state = {'sources': {'alpaca': {'watermark': '2026-10-23'}}, 'provider_status': {}}
    script.publish_index('gh', 'a/b', commit, state, tmp_path)
    assert len(writes) == 1
    assert json.loads(base64.b64decode(current['content']))['tag'] == commit.tag


def test_runtime_download_error_keeps_http_and_retries_reads(monkeypatch):
    from types import SimpleNamespace
    from backtest_data.remote import releases
    calls = []
    def failed(*args, **options):
        calls.append(args)
        return SimpleNamespace(returncode=1, stdout=b'', stderr=b'private text (HTTP 502)')
    monkeypatch.setattr(releases.subprocess, 'run', failed)
    monkeypatch.setattr(releases.time, 'sleep', lambda _: None)
    with pytest.raises(releases.GitHubOperationError) as caught:
        releases.github('gh', ['api', 'repos/a/b/releases'])
    assert caught.value.safe_details() == {'http_status': 502, 'operation': 'runtime_read'}
    assert len(calls) == 4 and 'private text' not in str(caught.value)
