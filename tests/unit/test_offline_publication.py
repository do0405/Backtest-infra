import importlib.util
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest


def module():
    path = Path(__file__).resolve().parents[2] / 'scripts/offline_publication.py'
    spec = importlib.util.spec_from_file_location('offline_publication_test', path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def asset(name, content, identity=1):
    return {'id': identity, 'name': name, 'size': len(content), 'state': 'uploaded',
            'digest': 'sha256:' + sha256(content).hexdigest()}


class Remote:
    def __init__(self, files):
        self.files = files
        self.release = {'id': 101, 'tag_name': 'offline-core-forward-v1-000000101',
                        'draft': True, 'prerelease': False, 'assets': []}
        self.calls = []
        self.upload_failure = False
        self.publish_failure = False

    def __call__(self, endpoint, *, method='GET', body=None, file=None):
        self.calls.append((method, endpoint))
        if method == 'GET':
            return json.loads(json.dumps(self.release))
        if method == 'POST' and '/assets?name=' in endpoint:
            name = endpoint.split('name=')[1]
            result = asset(name, Path(file).read_bytes(), len(self.release['assets']) + 1)
            self.release['assets'].append(result)
            if self.upload_failure:
                self.upload_failure = False
                raise self.error(502, 'asset_upload')
            return result
        if method == 'PATCH':
            self.release['draft'] = False
            if self.publish_failure:
                self.publish_failure = False
                raise self.error(502, 'release_publish')
            return self.release
        raise AssertionError((method, endpoint))


def test_partial_draft_keeps_existing_assets_and_uploads_only_missing(tmp_path):
    m = module()
    files = {'state.json.gz': b'state', 'captures-0001.zip': b'captures', 'commit.json': b'commit'}
    remote = Remote(files)
    remote.error = m.GitHubError
    remote.release['assets'] = [asset(name, files[name], i) for i, name in enumerate(list(files)[:2], 1)]
    before = list(remote.release['assets'])
    result = m.publish_files(remote, 'a/b', remote.release, files, tmp_path, sleep=lambda _: None)
    assert result['draft'] is False
    assert remote.release['assets'][:2] == before
    assert [url for method, url in remote.calls if method == 'POST'] == [
        'https://uploads.github.com/repos/a/b/releases/101/assets?name=commit.json']
    assert all(method != 'DELETE' for method, _ in remote.calls)


@pytest.mark.parametrize('phase', ['upload_failure', 'publish_failure'])
def test_accepted_write_with_lost_response_is_read_back_without_duplication(tmp_path, phase):
    m = module()
    files = {'commit.json': b'commit'}
    remote = Remote(files)
    remote.error = m.GitHubError
    setattr(remote, phase, True)
    m.publish_files(remote, 'a/b', remote.release, files, tmp_path, sleep=lambda _: None)
    assert len(remote.release['assets']) == 1
    assert sum(method == 'POST' for method, _ in remote.calls) == 1
    assert sum(method == 'PATCH' for method, _ in remote.calls) == 1


@pytest.mark.parametrize('mutation', ['digest', 'unexpected', 'starter', 'published', 'identity'])
def test_conflicting_or_published_release_is_never_mutated(tmp_path, mutation):
    m = module()
    files = {'commit.json': b'commit'}
    remote = Remote(files)
    remote.release['assets'] = [asset('commit.json', b'commit')]
    if mutation == 'digest': remote.release['assets'][0]['digest'] = 'sha256:' + '0' * 64
    if mutation == 'unexpected': remote.release['assets'].append(asset('other', b'other', 2))
    if mutation == 'starter': remote.release['assets'][0]['state'] = 'starter'
    if mutation == 'published': remote.release['draft'] = False
    if mutation == 'identity': remote.release['id'] = 102
    original = {'id': 101, 'tag_name': 'offline-core-forward-v1-000000101', 'draft': True}
    with pytest.raises(ValueError):
        m.publish_files(remote, 'a/b', original, files, tmp_path, sleep=lambda _: None)
    assert all(method == 'GET' for method, _ in remote.calls)


def test_safe_error_keeps_http_and_stage_without_arbitrary_stderr():
    m = module()
    response = SimpleNamespace(returncode=1, stdout=b'HTTP/2.0 502 Bad Gateway\r\n\r\n{}',
                               stderr=b'private credential text (HTTP 502)')
    with pytest.raises(m.GitHubError) as caught:
        m.github('gh', 'repos/a/b/releases/101', run=lambda *a, **k: response,
                 sleep=lambda _: None)
    assert caught.value.status == 502
    assert caught.value.operation == 'release_read'
    assert 'credential text' not in str(caught.value)


def test_transient_read_is_retried_but_authentication_is_not():
    m = module()
    responses = iter([SimpleNamespace(returncode=1, stdout=b'', stderr=b'HTTP 503'),
                      SimpleNamespace(returncode=0, stdout=b'{"id":101}', stderr=b'')])
    assert m.github('gh', 'repos/a/b/releases/101', run=lambda *a, **k: next(responses),
                    sleep=lambda _: None) == {'id': 101}
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1, stdout=b'', stderr=b'HTTP 403 permission denied')
    with pytest.raises(m.GitHubError) as caught:
        m.github('gh', 'repos/a/b/releases/101', run=forbidden, sleep=lambda _: None)
    assert caught.value.status == 403
    assert len(calls) == 1


def test_write_is_not_blindly_retried():
    m = module()
    calls = []
    def failed(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1, stdout=b'', stderr=b'HTTP 502')
    with pytest.raises(m.GitHubError):
        m.github('gh', 'repos/a/b/releases/101', method='PATCH', body={'draft': False},
                 run=failed, sleep=lambda _: None)
    assert len(calls) == 1


def test_rate_limit_403_respects_server_wait_and_does_not_hide_authentication():
    m = module()
    responses = iter([SimpleNamespace(returncode=1,
        stdout=b'HTTP/2.0 403 Forbidden\r\nRetry-After: 11\r\n\r\n{}', stderr=b'HTTP 403'),
        SimpleNamespace(returncode=0, stdout=b'{"id":101}', stderr=b'')])
    waits = []
    assert m.github('gh', 'repos/a/b/releases/101', run=lambda *a, **k: next(responses), sleep=waits.append) == {'id':101}
    assert waits == [11]


def test_complete_pending_draft_does_not_publish_a_tag(tmp_path):
    m = module()
    remote = Remote({'pending.zip': b'exact pending'})
    remote.error = m.GitHubError
    result = m.publish_files(remote, 'a/b', remote.release, {'pending.zip': b'exact pending'},
                             tmp_path, publish=False, commit_name='pending.zip')
    assert result['draft'] is True
    assert all(method != 'PATCH' for method, _ in remote.calls)


def test_unknown_cli_errors_are_not_silently_deferred():
    m = module()
    calls = []
    def invalid(*a,**k):
        calls.append(a)
        return SimpleNamespace(returncode=1,stdout=b'',stderr=b'unknown flag: --invalid')
    with pytest.raises(m.GitHubError) as caught:
        m.github('gh','repos/a/b/releases/101',run=invalid,sleep=lambda _:None)
    assert not caught.value.retryable and len(calls)==1
    assert caught.value.safe_details()['reason']=='cli_error'


def test_missing_cli_is_a_configuration_failure():
    m = module()
    def missing(*a,**k):raise FileNotFoundError('gh')
    with pytest.raises(ValueError,match='cli_unavailable'):
        m.github('gh','repos/a/b/releases/101',run=missing,sleep=lambda _:None)
