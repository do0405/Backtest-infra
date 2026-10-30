"""Persist exact capture deltas before publication; recover them before acquisition."""
import gzip
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import re
import stat
from tempfile import TemporaryDirectory
import zipfile

import offline_publication as publication
from offline_publication import GitHubError, _verify, publish_files

MAX_PENDING_BYTES = 512 * 1024**2
FIELDS = {'format', 'stream_id', 'catalog_sha256', 'sequence', 'commit_sha256',
          'package_sha256', 'package_bytes'}


def output(**values):
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with Path(path).open('a', encoding='utf-8') as target:
            for key, value in values.items():
                if not re.fullmatch(r'[a-z_]+', key) or not re.fullmatch(r'[a-z0-9_]+', str(value)):
                    raise ValueError('safe workflow output required')
                target.write(f'{key}={value}\n')


def pending_tag(stream, sequence, digest):
    return f'offline-pending-{stream}-{sequence:09d}-{digest}'


def validate(files, *, stream, catalog_sha256):
    from backtest_data.offline.archives import read_bundle
    from backtest_data.offline.contracts import decode, MAX_STATE_BYTES, MAX_MANIFEST_BYTES
    from backtest_data.offline.transport import read_commit, release_tag
    if ('commit.json' not in files or len(files) > 34 or
            sum(map(len, files.values())) > MAX_PENDING_BYTES):
        raise ValueError('bounded complete pending files required')
    if not 0 < len(files['commit.json']) <= MAX_MANIFEST_BYTES:
        raise ValueError('bounded pending commit required')
    raw = decode(files['commit.json'])
    manifest = read_commit(files['commit.json'], stream=stream, catalog_sha256=catalog_sha256,
                           tag=release_tag(stream, raw['sequence']))
    if set(files) != {'commit.json', *manifest['files']}:
        raise ValueError('exact pending membership required')
    captures = []
    for name, record in manifest['files'].items():
        content = files[name]
        if len(content) != record['bytes'] or sha256(content).hexdigest() != record['sha256']:
            raise ValueError('pending file hash mismatch')
        if name != 'state.json.gz':
            captures.extend({'asset': name, 'capture': capture} for capture, _ in read_bundle(content))
    if captures != manifest['captures']: raise ValueError('pending capture membership mismatch')
    with gzip.GzipFile(fileobj=io.BytesIO(files['state.json.gz'])) as source:
        raw_state = source.read(MAX_STATE_BYTES + 1)
    if len(raw_state) > MAX_STATE_BYTES: raise ValueError('pending state expansion bound exceeded')
    state = decode(raw_state)
    if not isinstance(state, dict) or state.get('catalog_sha256', catalog_sha256) != catalog_sha256:
        raise ValueError('pending state catalog mismatch')
    return manifest, state


def pack(files, path):
    """Only the current delta; no SQL database, credentials or historical export."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.part')
    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system, info.compress_type = 0, zipfile.ZIP_DEFLATED
            archive.writestr(info, files[name])
    if temporary.stat().st_size > MAX_PENDING_BYTES: raise ValueError('pending package bound exceeded')
    temporary.replace(path)
    return path.read_bytes()


def unpack(raw, *, stream, catalog_sha256):
    if not 0 < len(raw) <= MAX_PENDING_BYTES: raise ValueError('bounded pending package required')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            names = [item.filename for item in entries]
            if (len(set(names)) != len(names) or not 2 <= len(names) <= 34 or
                    sum(item.file_size for item in entries) > MAX_PENDING_BYTES):
                raise ValueError('bounded unique pending members required')
            for item in entries:
                if (not re.fullmatch(r'commit\.json|state\.json\.gz|captures-\d{4}\.zip', item.filename) or
                        item.flag_bits & 1 or stat.S_ISLNK(item.external_attr >> 16) or
                        item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                    raise ValueError('safe pending members required')
            files = {name: archive.read(name) for name in names}
        manifest, state = validate(files, stream=stream, catalog_sha256=catalog_sha256)
        return manifest, files, state
    except (zipfile.BadZipFile, KeyError, TypeError, EOFError, gzip.BadGzipFile) as exc:
        raise ValueError('invalid pending package') from exc


def binding(release, transport):
    try:
        value = json.loads(release.get('body') or '{}')
        if (set(value) != FIELDS or value['format'] != 'offline_publication_pending_v1' or
                value['stream_id'] != transport.stream or value['catalog_sha256'] != transport.catalog_sha256 or
                type(value['sequence']) is not int or value['sequence'] < 1 or
                type(value['package_bytes']) is not int or not 0 < value['package_bytes'] <= MAX_PENDING_BYTES or
                any(not re.fullmatch(r'[a-f0-9]{64}', value[key]) for key in ('commit_sha256', 'package_sha256')) or
                release['tag_name'] != pending_tag(transport.stream, value['sequence'], value['commit_sha256'])):
            raise ValueError('exact pending identity required')
        return value
    except (TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError('invalid pending binding') from exc


def ensure_release(transport, tag, body):
    api = lambda endpoint, **options: publication.github(transport.gh, endpoint, **options)
    matches = [r for r in transport._inventory() if r.get('tag_name') == tag]
    if len(matches) > 1: raise ValueError('unique release identity required')
    if matches: return matches[0]
    try:
        release = api(f'repos/{transport.repository}/releases', method='POST', body={
            'tag_name': tag, 'target_commitish': 'main', 'draft': True, 'prerelease': False,
            'name': tag, 'body': body})
        transport._remember_release(release)
        return release
    except GitHubError:
        matches = [r for r in transport._refresh_inventory() if r.get('tag_name') == tag]
        if len(matches) != 1: raise
        return matches[0]


def persist(transport, manifest, files, *, package=None):
    validate(files, stream=transport.stream, catalog_sha256=transport.catalog_sha256)
    if package is None:
        raw = pack(files, transport.workspace.parent/'pending.zip')
    else:
        saved_manifest, saved_files, _ = unpack(package, stream=transport.stream, catalog_sha256=transport.catalog_sha256)
        if saved_manifest != manifest or saved_files != files: raise ValueError('original fallback bytes required')
        raw = package
        (transport.workspace.parent/'pending.zip').write_bytes(raw)
    value = {'format': 'offline_publication_pending_v1', 'stream_id': transport.stream,
             'catalog_sha256': transport.catalog_sha256, 'sequence': manifest['sequence'],
             'commit_sha256': sha256(files['commit.json']).hexdigest(),
             'package_sha256': sha256(raw).hexdigest(), 'package_bytes': len(raw)}
    output(pending_sequence=str(value['sequence']).zfill(9), pending_sha256=value['commit_sha256'])
    tag = pending_tag(transport.stream, value['sequence'], value['commit_sha256'])
    release = ensure_release(transport, tag, json.dumps(value, sort_keys=True))
    if binding(release, transport) != value: raise ValueError('pending identity conflict; preserved')
    api = lambda endpoint, **options: publication.github(transport.gh, endpoint, **options)
    if release.get('draft') is False:
        if _verify(release, release, {'pending.zip': raw}, published=True) != {'pending.zip'}:
            raise ValueError('complete durable pending package required')
        return release
    with TemporaryDirectory(prefix='pending-upload-', dir=transport.workspace) as directory:
        saved = publish_files(api, transport.repository, release, {'pending.zip': raw},
                              directory, commit_name='pending.zip', publish=False)
    transport._remember_release(saved)
    return saved


def remove_local(transport, pending):
    path = transport.workspace.parent/'pending.zip'
    if path.exists():
        if sha256(path.read_bytes()).hexdigest() != binding(pending, transport)['package_sha256']:
            raise ValueError('local pending changed; preserved')
        path.unlink()


def artifact_packages(transport, inventory):
    """Fallback only: trusted core runs, exact package digest, no executable extraction."""
    from backtest_data.remote.releases import github as download
    api = lambda endpoint, **options: publication.github(transport.gh, endpoint, **options)
    prefix = f'repos/{transport.repository}'
    pattern = re.compile(r'offline-core-pending-v1-s(\d{9})-h([a-f0-9]{64})-r(\d+)-a(\d+)$')
    packages = []
    for page in range(1, 101):
        artifacts = api(prefix+f'/actions/artifacts?per_page=100&page={page}')['artifacts']
        for artifact in artifacts:
            match = pattern.fullmatch(artifact.get('name', ''))
            if not match or artifact.get('expired'): continue
            sequence, digest, run_id, attempt = int(match[1]), match[2], int(match[3]), int(match[4])
            tag = f'offline-{transport.stream}-{sequence:09d}'
            committed = next((r for r in inventory if r.get('tag_name') == tag and not r.get('draft')), None)
            if committed and any(a.get('name') == 'commit.json' and a.get('digest') == 'sha256:'+digest
                                 for a in committed.get('assets', [])):
                continue
            origin = artifact.get('workflow_run', {})
            run = api(prefix+f'/actions/runs/{run_id}')
            if (origin.get('id') != run_id or origin.get('head_branch') != 'main' or
                    type(origin.get('repository_id')) is not int or origin['repository_id'] < 1 or
                    origin['repository_id'] != run.get('repository', {}).get('id') or
                    origin.get('head_repository_id') != origin.get('repository_id') or
                    run.get('repository', {}).get('full_name') != transport.repository or
                    run.get('path') != '.github/workflows/offline-core-refresh.yml' or
                    run.get('head_branch') != 'main' or run.get('head_sha') != origin.get('head_sha') or
                    run.get('event') not in {'schedule', 'workflow_dispatch', 'workflow_run'} or
                    not 1 <= attempt <= run.get('run_attempt', 0)):
                continue
            if (type(artifact.get('id')) is not int or artifact['id'] < 1 or
                    type(artifact.get('size_in_bytes')) is not int or
                    not 0 < artifact['size_in_bytes'] <= MAX_PENDING_BYTES or
                    not re.fullmatch(r'sha256:[a-f0-9]{64}', artifact.get('digest', ''))):
                raise ValueError('bounded authenticated fallback artifact required')
            with TemporaryDirectory(prefix='pending-artifact-', dir=transport.workspace) as directory:
                path = Path(directory)/'artifact.zip'
                download(transport.gh, ['api', prefix+f'/actions/artifacts/{artifact["id"]}/zip'], output=path, timeout=300)
                raw = path.read_bytes()
            if len(raw) != artifact['size_in_bytes'] or 'sha256:'+sha256(raw).hexdigest() != artifact['digest']:
                raise ValueError('fallback artifact digest mismatch')
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = archive.infolist()
                if (len(entries) != 1 or entries[0].filename != 'pending.zip' or
                        not 0 < entries[0].file_size <= MAX_PENDING_BYTES or entries[0].flag_bits & 1 or
                        stat.S_ISLNK(entries[0].external_attr >> 16)):
                    raise ValueError('single bounded fallback pending file required')
                package = archive.read('pending.zip')
            manifest, files, state = unpack(package, stream=transport.stream, catalog_sha256=transport.catalog_sha256)
            if manifest['sequence'] != sequence or sha256(files['commit.json']).hexdigest() != digest:
                raise ValueError('fallback artifact commit binding mismatch')
            packages.append((manifest, files, state, package))
        if len(artifacts) < 100: break
    else: raise ValueError('bounded fallback inventory exceeded')
    return packages


def acknowledge(transport, pending, commit, files):
    """Remove only this transaction's duplicate after canonical and index verification."""
    api = lambda endpoint, **options: publication.github(transport.gh, endpoint, **options)
    value = binding(pending, transport)
    if value['commit_sha256'] != commit.sha256 or sha256(files['commit.json']).hexdigest() != commit.sha256:
        raise ValueError('exact committed pending hash required')
    canonical = api(f'repos/{transport.repository}/releases/{commit.release["id"]}')
    if canonical.get('draft') is not False or _verify(canonical, commit.release, files, published=True) != set(files):
        raise ValueError('complete canonical readback required before acknowledgement')
    current = api(f'repos/{transport.repository}/releases/{pending["id"]}')
    if binding(current, transport) != value or current.get('draft') is not True:
        raise ValueError('owned unpublished pending required before acknowledgement')
    try: api(f'repos/{transport.repository}/releases/{pending["id"]}', method='DELETE')
    except GitHubError as exc:
        if exc.status != 404:
            if not exc.retryable: raise
            print(json.dumps({'status': 'pending_ack_deferred', **exc.safe_details()}), flush=True)
            return False
    transport._forget_release(pending['id'])
    return True


def recover(transport, *, on_commit, include_artifacts=False):
    """Resolve saved publications before source calls; refuse conflicting generations."""
    inventory = transport._inventory()
    pending = [r for r in inventory
               if r.get('tag_name', '').startswith('offline-pending-'+transport.stream+'-')]
    bound = [(binding(release, transport), release) for release in pending]
    fallback = artifact_packages(transport, inventory) if include_artifacts else []
    generations = {}
    for value, _ in bound:
        if value['sequence'] in generations and generations[value['sequence']] != value['commit_sha256']:
            raise ValueError('conflicting pending generations; all evidence preserved')
        generations[value['sequence']] = value['commit_sha256']
    for manifest, files, _, _ in fallback:
        digest = sha256(files['commit.json']).hexdigest()
        if manifest['sequence'] in generations and generations[manifest['sequence']] != digest:
            raise ValueError('conflicting pending fallback generations; preserved')
        generations[manifest['sequence']] = digest
    for manifest, files, _, package in fallback:
        persist(transport, manifest, files, package=package)
    if fallback:
        return recover(transport, on_commit=on_commit, include_artifacts=False)
    recovered, verified = [], []
    for value, release in sorted(bound, key=lambda item: item[0]['sequence']):
        record = {'bytes': value['package_bytes'], 'sha256': value['package_sha256']}
        with TemporaryDirectory(prefix='pending-read-', dir=transport.workspace) as directory:
            path = transport._download(release, 'pending.zip', directory, bound=MAX_PENDING_BYTES, record=record)
            raw = path.read_bytes()
        if len(raw) != record['bytes'] or sha256(raw).hexdigest() != record['sha256']:
            raise ValueError('pending package hash mismatch')
        manifest, files, state = unpack(raw, stream=transport.stream, catalog_sha256=transport.catalog_sha256)
        if manifest['sequence'] != value['sequence'] or sha256(files['commit.json']).hexdigest() != value['commit_sha256']:
            raise ValueError('pending commit binding mismatch')
        commit = transport.publish_prepared(manifest, files, persist=False)
        recovered.append(commit)
        verified.append((release, commit, files, state))
    if verified:
        head = transport.last_committed()
        selected = next((item for item in verified if item[1].sha256 == head.sha256), None)
        if selected: state = selected[3]
        else:
            with TemporaryDirectory(prefix='pending-head-', dir=transport.workspace) as directory:
                state = transport._read_state(head, directory)
        on_commit(head, state)
        for release, commit, files, _ in verified: acknowledge(transport, release, commit, files)
    head = transport.last_committed()
    next_tag = f'offline-{transport.stream}-{(head.sequence+1 if head else 1):09d}'
    if any(r.get('tag_name') == next_tag and r.get('draft') for r in transport._inventory()):
        raise ValueError('unresolved draft requires saved pending package before acquisition')
    return recovered
