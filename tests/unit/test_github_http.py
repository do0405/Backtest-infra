"""Offline contracts for the shared GitHub CLI transport."""
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest


def reply(monkeypatch, module, stdout, *, stderr=b'', returncode=0):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    monkeypatch.setattr(module.subprocess, 'run', run)
    return calls


def test_raw_asset_preserves_crlf_and_header_like_body(monkeypatch):
    from backtest_data.remote import github_http as module
    body = b'HTTP/1.1 original asset\r\n\r\n\x00unchanged\r\n'
    reply(monkeypatch, module, b'HTTP/2.0 200 OK\r\nContent-Type: application/octet-stream\r\n\r\n' + body)
    response = module.GhAPI('gh').request('GET', 'repos/a/b/releases/assets/1', raw=True)
    assert response.status == 200 and response.data == body


def test_redirect_and_interim_headers_use_final_response(monkeypatch):
    from backtest_data.remote import github_http as module
    wire = (b'HTTP/1.1 100 Continue\r\n\r\n'
            b'HTTP/2.0 302 Found\nLocation: private\n\n'
            b'HTTP/2.0 200 OK\r\nX-RateLimit-Remaining: 9\r\n\r\n{"ok":true}')
    reply(monkeypatch, module, wire)
    response = module.GhAPI('gh').request('GET', 'repos/a/b')
    assert response.status == 200 and response.data == {'ok': True}
    assert response.headers == {'x-ratelimit-remaining': '9'}


def test_cli_failure_without_http_status_remains_unknown(monkeypatch):
    from backtest_data.remote import github_http as module
    reply(monkeypatch, module, b'', stderr=b'HTTP 403 authentication PRIVATE_TOKEN', returncode=1)
    response = module.GhAPI('gh').request('GET', 'repos/a/b')
    assert response.status == 0 and response.data is None
    assert 'PRIVATE_TOKEN' not in repr(response)


def test_explicit_transport_timeout_and_upload_length(monkeypatch, tmp_path):
    from backtest_data.remote import github_http as module
    calls = reply(monkeypatch, module, b'HTTP/2.0 201 Created\n\n{"id":1}')
    path = tmp_path / 'asset.bin'
    path.write_bytes(b'exact upload')
    module.GhAPI('gh').request('POST', 'uploads', input_file=path, timeout=0.25)
    command, options = calls[0]
    assert options['timeout'] == 0.25
    assert command[command.index('--input') + 1] == str(path)
    assert 'Content-Length: 12' in command


@pytest.mark.parametrize('wire', [b'HTTP/2.0 200 OK\nBroken\n\n{}',
                                  b'HTTP/2.0 200 OK\n\nnot JSON'])
def test_bad_success_proof_raises_safe_error(monkeypatch, wire):
    from backtest_data.remote import github_http as module
    reply(monkeypatch, module, wire, stderr=b'PRIVATE_TOKEN')
    with pytest.raises(ValueError) as caught:
        module.GhAPI('gh').request('GET', 'repos/a/b')
    assert 'PRIVATE_TOKEN' not in str(caught.value)


def test_bounded_control_transport_uses_remaining_time(monkeypatch):
    from backtest_data.remote import github_http as module
    from backtest_data.remote.krx_publication import BoundedGhAPI
    calls = reply(monkeypatch, module, b'HTTP/2.0 200 OK\n\n{}')
    api = BoundedGhAPI('gh', budget_seconds=0.5, clock=lambda: 10)
    assert api.get('repos/a/b') == {}
    assert calls[0][1]['timeout'] == 0.5 and api.count == 1


def test_publication_inventory_hashes_in_chunks(monkeypatch, tmp_path):
    from backtest_data.remote.paced_publication import publication_assets
    path = tmp_path / 'archive.zip'
    body = b'original archive' * 100000
    path.write_bytes(body)
    monkeypatch.setattr(Path, 'read_bytes', lambda _: pytest.fail('whole-file hash allocation'))
    assets = publication_assets([path], max_assets=3)
    assert assets[path.name]['digest'] == 'sha256:' + hashlib.sha256(body).hexdigest()
    assert assets[path.name]['size'] == len(body)


def test_invalid_control_inventory_is_rejected_before_hashing(monkeypatch, tmp_path):
    from backtest_data.remote import krx_publication as module
    paths = [tmp_path / f'asset-{i}.zip' for i in range(4)]
    for path in paths:
        path.write_bytes(b'original')
    reads = []
    original = Path.open

    def opened(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', opened)
    with pytest.raises(module.PublicationFailure):
        module.publish_control('gh', 'a/b', 'data-test', paths, source_sha='a' * 40,
                               run_id='17', run_attempt=2, title='test', notes='test')
    assert reads == []
