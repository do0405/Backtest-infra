"""Recovery must use the exact saved publication, before any new acquisition."""
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import sys
import zipfile

import pytest

source = os.environ.get('OFFLINE_RUNTIME_SOURCE')
if not source: pytest.skip('OFFLINE_RUNTIME_SOURCE required', allow_module_level=True)
runtime = Path(source).resolve()
sys.path[:0] = [str(runtime/'scripts'), str(runtime/'src')]
import offline_publication as publication
import offline_outbox as outbox
from backtest_data.offline import transport
from backtest_data.offline.archives import build_bundles
from backtest_data.offline.contracts import make_capture


class Remote:
    def __init__(self):
        self.releases, self.contents, self.calls = {}, {}, []
        self.fail_commit = False

    def inventory(self): return json.loads(json.dumps(list(self.releases.values())))

    def __call__(self, gh, endpoint, *, method='GET', body=None, file=None):
        self.calls.append((method, endpoint))
        if method == 'POST' and endpoint.endswith('/releases'):
            identity = max(self.releases, default=0) + 1
            value = {'id': identity, 'tag_name': body['tag_name'], 'body': body.get('body', ''),
                     'draft': True, 'prerelease': False, 'assets': []}
            self.releases[identity] = value
        elif '/assets?name=' in endpoint:
            identity = int(endpoint.split('/releases/')[1].split('/')[0])
            name = endpoint.split('name=')[1]
            value = self.releases[identity]
            if self.fail_commit and name == 'commit.json' and value['tag_name'].startswith('offline-core-'):
                raise publication.GitHubError(503, 'asset_upload')
            raw = Path(file).read_bytes()
            asset_id = len(self.contents) + 1
            self.contents[asset_id] = raw
            value['assets'].append({'id': asset_id, 'name': name, 'state': 'uploaded',
                                    'size': len(raw), 'digest': 'sha256:' + sha256(raw).hexdigest()})
        else:
            identity = int(endpoint.rsplit('/', 1)[1])
            if method == 'DELETE':
                del self.releases[identity]
                return None
            value = self.releases[identity]
            if method == 'PATCH': value['draft'] = False
        return json.loads(json.dumps(value))


def publication_files():
    raw = b'{"bars":[]}'
    capture = make_capture(source='alpaca', kind='alpaca_bars',
        entity_id='00179df8-84f3-5ed8-9ea6-469a85bf94aa',
        captured_at=datetime(2026, 10, 30, tzinfo=timezone.utc), content=raw, metadata={})
    state = {'catalog_sha256': 'a'*64, 'sources': {}, 'provider_status': {}}
    manifest, files = transport.prepare_release(build_bundles([(capture, raw)]), state,
        stream='core-forward-v1', catalog_sha256='a'*64, sequence=1)
    return manifest, files, state


def configured(tmp_path, monkeypatch, remote):
    monkeypatch.setattr(publication, 'github', remote)
    monkeypatch.setattr(publication.time, 'sleep', lambda _: None)
    monkeypatch.setitem(publication.publish_files.__kwdefaults__, 'sleep', lambda _: None)
    value = transport.GitHubCaptureTransport('a/b', workspace=tmp_path/'transport', catalog_sha256='a'*64)
    monkeypatch.setattr(value, '_inventory', remote.inventory)
    monkeypatch.setattr(value, '_download', lambda release, name, directory, **kwargs: downloaded(remote, release, name, directory))
    return value


def downloaded(remote, release, name, directory):
    asset = next(a for a in release['assets'] if a['name'] == name)
    path = Path(directory)/name
    path.write_bytes(remote.contents[asset['id']])
    return path


def test_pending_is_complete_and_durable_before_canonical_upload(tmp_path, monkeypatch):
    remote = Remote()
    value = configured(tmp_path, monkeypatch, remote)
    manifest, files, state = publication_files()
    commit = value.publish_prepared(manifest, files)
    pending = next(r for r in remote.releases.values() if r['tag_name'].startswith('offline-pending-'))
    assert pending['draft'] is True
    asset = pending['assets'][0]
    saved_manifest, saved_files, saved_state = outbox.unpack(remote.contents[asset['id']], stream=value.stream, catalog_sha256='a'*64)
    assert saved_manifest == manifest and saved_files == files and saved_state == state
    creations = [i for i, (method, url) in enumerate(remote.calls) if method == 'POST' and url.endswith('/releases')]
    assert len(creations) == 2
    assert any('/assets?name=pending.zip' in url for _, url in remote.calls[:creations[1]])
    assert commit.sha256 == sha256(files['commit.json']).hexdigest()


def test_restart_recovers_missing_commit_without_recollection_or_reencoding(tmp_path, monkeypatch):
    remote = Remote()
    first = configured(tmp_path/'first', monkeypatch, remote)
    manifest, files, state = publication_files()
    remote.fail_commit = True
    with pytest.raises(publication.GitHubError): first.publish_prepared(manifest, files)
    before = {a['name']: a['id'] for r in remote.releases.values()
              if r['tag_name'] == 'offline-core-forward-v1-000000001' for a in r['assets']}
    remote.fail_commit = False
    second = configured(tmp_path/'restart', monkeypatch, remote)
    observed = []
    recovered = outbox.recover(second, on_commit=lambda commit, recovered_state: observed.append((commit, recovered_state)))
    assert len(recovered) == 1 and observed[0][1] == state
    assert recovered[0].sha256 == sha256(files['commit.json']).hexdigest()
    canonical = next(r for r in remote.releases.values() if r['tag_name'] == 'offline-core-forward-v1-000000001')
    assert canonical['draft'] is False
    assert {a['name']: a['id'] for a in canonical['assets'] if a['name'] in before} == before
    assert all(not r['tag_name'].startswith('offline-pending-') for r in remote.releases.values())


def test_index_failure_keeps_pending_until_next_recovery(tmp_path, monkeypatch):
    remote = Remote()
    value = configured(tmp_path, monkeypatch, remote)
    manifest, files, state = publication_files()
    value.publish_prepared(manifest, files)
    def unavailable(*args): raise publication.GitHubError(503, 'repository_write')
    with pytest.raises(publication.GitHubError): outbox.recover(value, on_commit=unavailable)
    assert any(r['tag_name'].startswith('offline-pending-') for r in remote.releases.values())
    outbox.recover(value, on_commit=lambda *args: None)
    assert len(remote.releases) == 1


@pytest.mark.parametrize('mutation', ['capture_hash', 'state_hash', 'extra', 'traversal', 'duplicate'])
def test_tampered_or_unsafe_pending_is_rejected(tmp_path, mutation):
    manifest, files, state = publication_files()
    if mutation == 'capture_hash': files['captures-0001.zip'] += b'changed'
    if mutation == 'state_hash': files['state.json.gz'] += b'changed'
    if mutation == 'extra': files['execute.py'] = b'raise RuntimeError()'
    if mutation == 'traversal': files['../escape'] = b'bad'
    target = io.BytesIO()
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in files.items(): archive.writestr(name, raw)
        if mutation == 'duplicate':
            with pytest.warns(UserWarning, match='Duplicate name'):
                archive.writestr('commit.json', files['commit.json'])
    with pytest.raises(ValueError): outbox.unpack(target.getvalue(), stream='core-forward-v1', catalog_sha256='a'*64)


def test_acknowledgement_refuses_changed_canonical_hash(tmp_path, monkeypatch):
    remote = Remote()
    value = configured(tmp_path, monkeypatch, remote)
    manifest, files, state = publication_files()
    commit = value.publish_prepared(manifest, files)
    pending = next(r for r in remote.releases.values() if r['tag_name'].startswith('offline-pending-'))
    canonical = remote.releases[commit.release['id']]
    canonical['assets'][0]['digest'] = 'sha256:' + '0'*64
    with pytest.raises(ValueError): outbox.acknowledge(value, pending, commit, files)
    assert pending['id'] in remote.releases


def test_conflicting_pending_generations_stop_before_any_write(tmp_path, monkeypatch):
    remote = Remote()
    value = configured(tmp_path, monkeypatch, remote)
    manifest, files, state = publication_files()
    value.publish_prepared(manifest, files)
    duplicate = next(r for r in remote.inventory() if r['tag_name'].startswith('offline-pending-'))
    duplicate['id'] = 99
    binding = json.loads(duplicate['body'])
    binding['commit_sha256'] = 'b'*64
    duplicate['body'] = json.dumps(binding)
    duplicate['tag_name'] = outbox.pending_tag('core-forward-v1', 1, 'b'*64)
    remote.releases[99] = duplicate
    remote.calls.clear()
    with pytest.raises(ValueError, match='conflicting'): outbox.recover(value, on_commit=lambda *args: None)
    assert all(method == 'GET' for method, _ in remote.calls)


def test_inventory_is_reused_only_within_one_transport_and_reconciled_after_writes(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(transport, 'github', lambda *args, **kwargs: calls.append(args) or b'[[]]')
    first = transport.GitHubCaptureTransport('a/b', workspace=tmp_path/'one', catalog_sha256='a'*64)
    assert first._inventory() == first._inventory() == [] and len(calls) == 1
    release = {'id':1,'tag_name':'temporary'}
    first._remember_release(release)
    assert first._inventory() == [release] and len(calls) == 1
    first._forget_release(1)
    assert first._inventory() == []
    first._refresh_inventory()
    assert len(calls) == 2
    second = transport.GitHubCaptureTransport('a/b', workspace=tmp_path/'two', catalog_sha256='a'*64)
    second._inventory()
    assert len(calls) == 3


def test_recovery_precedes_provider_credentials_and_credit_reservation(tmp_path, monkeypatch):
    import importlib.util
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('recovery_order_test', runtime/'scripts/offline_refresh.py')
    script = importlib.util.module_from_spec(spec); spec.loader.exec_module(script)
    raw = b'{"version":1}'
    catalog_hash = sha256(raw).hexdigest()
    monkeypatch.setenv('OFFLINE_CONFIGURATION', json.dumps({'catalog_tag':'catalog','catalog_sha256':catalog_hash,'stream_id':'core-forward-v1'}))
    monkeypatch.setenv('REPOSITORY','a/b')
    monkeypatch.setattr(sys,'argv',['offline_refresh.py','--work',str(tmp_path),'--budget-seconds','60'])
    monkeypatch.setattr(script.shutil,'which',lambda _: 'gh')
    def bootstrap(gh, args, output=None):
        if output: Path(output).write_bytes(raw); return
        return json.dumps({'draft':False,'assets':[{'id':1,'name':'catalog.json','size':len(raw)}]}).encode()
    monkeypatch.setattr(script,'github',bootstrap)
    commit = SimpleNamespace(tag='offline-core-forward-v1-000000001')
    state = {'sources':{},'provider_status':{}}
    fake = SimpleNamespace(load_latest_verified_state=lambda: (commit,state))
    monkeypatch.setattr(script,'GitHubCaptureTransport',lambda *a,**k: fake)
    order = []
    monkeypatch.setattr(script,'recover',lambda *a,**k: order.append('recovered') or [commit])
    monkeypatch.setattr(script,'publish_index',lambda *a: order.append('index'))
    def forbidden(*a,**k):raise AssertionError('recovery must not call a provider or reserve credits')
    monkeypatch.setattr(script,'reserve_hosted',forbidden)
    monkeypatch.setattr(script,'load_credentials',forbidden)
    script.main()
    assert order == ['recovered','index']


def test_fallback_provenance_and_digest_are_checked_before_restoring(tmp_path, monkeypatch):
    from backtest_data.remote import releases
    manifest, files, _ = publication_files()
    package = outbox.pack(files,tmp_path/'pending.zip')
    wrapper = io.BytesIO()
    with zipfile.ZipFile(wrapper,'w') as archive:archive.writestr('pending.zip',package)
    raw = wrapper.getvalue()
    run = {'id':7,'repository':{'id':11,'full_name':'a/b'},'path':'.github/workflows/offline-core-refresh.yml',
           'head_branch':'main','head_sha':'c'*40,'event':'schedule','run_attempt':1}
    artifact = {'id':8,'name':'offline-core-pending-v1-s000000001-h'+sha256(files['commit.json']).hexdigest()+'-r7-a1',
                'expired':False,'size_in_bytes':len(raw),'digest':'sha256:'+sha256(raw).hexdigest(),
                'workflow_run':{'id':7,'repository_id':11,'head_repository_id':11,'head_branch':'main','head_sha':'c'*40}}
    monkeypatch.setattr(publication,'github',lambda gh,url,**k: {'artifacts':[artifact]} if '/artifacts?' in url else run)
    monkeypatch.setattr(releases,'github',lambda gh,args,output,**k: Path(output).write_bytes(raw))
    value = transport.GitHubCaptureTransport('a/b',workspace=tmp_path/'transport',catalog_sha256='a'*64)
    restored = outbox.artifact_packages(value,[])
    assert len(restored)==1 and restored[0][1]==files and restored[0][3]==package
    artifact['digest']='sha256:'+'0'*64
    with pytest.raises(ValueError,match='digest'):outbox.artifact_packages(value,[])
    artifact['workflow_run']['head_repository_id']=99
    assert outbox.artifact_packages(value,[])==[]
