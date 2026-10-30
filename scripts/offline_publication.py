"""Bounded GitHub diagnostics and publication without deleting draft evidence."""
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import quote


class GitHubError(RuntimeError):
    def __init__(self, status=None, operation='github_request'):
        self.status, self.operation = status, operation
        super().__init__(json.dumps(self.safe_details(), sort_keys=True))

    def safe_details(self):
        return {'http_status': self.status, 'operation': self.operation}

    @property
    def retryable(self):
        return self.status is None or self.status in {408, 429, 500, 502, 503, 504}


def _operation(endpoint, method):
    if '/assets?name=' in endpoint: return 'asset_upload'
    if '/releases' in endpoint:
        return 'release_read' if method == 'GET' else 'release_publish' if method == 'PATCH' else 'release_create'
    if '/contents/' in endpoint: return 'repository_read' if method == 'GET' else 'repository_write'
    if '/actions/' in endpoint: return 'workflow_read' if method == 'GET' else 'workflow_update'
    return 'repository_read' if method == 'GET' else 'github_write'


def github(gh, endpoint, *, method='GET', body=None, file=None,
           run=subprocess.run, sleep=time.sleep):
    """Retry reads only; callers reconcile ambiguous writes with a fresh read."""
    if body is not None and file is not None: raise ValueError('one request body required')
    command = [str(gh), 'api', '--include', '--method', method, endpoint]
    content = None
    if body is not None:
        command += ['--input', '-']
        content = json.dumps(body).encode()
    if file is not None:
        command += ['-H', 'Content-Type: application/octet-stream', '--input', str(file)]
    for attempt in range(4 if method == 'GET' else 1):
        try:
            result = run(command, input=content, capture_output=True, timeout=120)
        except (subprocess.TimeoutExpired, OSError):
            error = GitHubError(None, _operation(endpoint, method))
        else:
            match = re.search(rb'HTTP(?:/[\d.]+)?\s+(\d{3})', result.stdout + b'\n' + (result.stderr or b''))
            status = int(match[1]) if match else None
            if result.returncode == 0 and (status is None or 200 <= status < 300):
                payload = result.stdout
                if payload.startswith(b'HTTP/'):
                    payload = re.split(rb'\r?\n\r?\n', payload, maxsplit=1)[-1]
                if len(payload) > 2 * 1024**2: raise ValueError('bounded_github_response_required')
                return json.loads(payload) if payload.strip() else None
            error = GitHubError(status, _operation(endpoint, method))
        if not error.retryable or method != 'GET' or attempt == 3: raise error
        sleep(2 ** (attempt + 1))
    raise AssertionError('bounded GitHub attempt loop')


def _verify(release, original, files, *, published=False):
    if (release.get('id') != original['id'] or release.get('tag_name') != original['tag_name'] or
            release.get('prerelease') is not False or
            release.get('draft') is not True and not published):
        raise ValueError('original unpublished release identity required')
    assets = release.get('assets')
    if not isinstance(assets, list): raise ValueError('explicit release assets required')
    names = [value.get('name') for value in assets]
    if len(set(names)) != len(names) or set(names) - set(files):
        raise ValueError('unexpected or duplicate draft assets; preserved for review')
    for value in assets:
        content = files[value['name']]
        if (type(value.get('id')) is not int or value['id'] <= 0 or
                value.get('state') != 'uploaded' or value.get('size') != len(content) or
                value.get('digest') != 'sha256:' + sha256(content).hexdigest()):
            raise ValueError('draft asset hash mismatch; original asset preserved')
    return set(names)


def publish_files(api, repository, release, files, work, *, sleep=time.sleep):
    """Upload only missing exact bytes; commit last, verify, then publish."""
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or
            type(release.get('id')) is not int or release['id'] <= 0 or
            not isinstance(files, dict) or 'commit.json' not in files or
            any(not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or not isinstance(raw, bytes)
                for name, raw in files.items())):
        raise ValueError('explicit repository, release and byte files required')
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    endpoint = f'repos/{repository}/releases/{release["id"]}'
    current = api(endpoint)
    existing = _verify(current, release, files)
    for name in [key for key in files if key != 'commit.json'] + ['commit.json']:
        if name in existing: continue
        path = work / name
        path.write_bytes(files[name])
        for attempt in range(4):
            current = api(endpoint)
            existing = _verify(current, release, files)
            if name in existing: break
            try:
                api(f'https://uploads.github.com/repos/{repository}/releases/{release["id"]}/assets?name=' +
                    quote(name, safe=''), method='POST', file=path)
            except GitHubError as exc:
                # Even the final failed response can have accepted the bytes.
                current = api(endpoint)
                if name in _verify(current, release, files): break
                if not exc.retryable or attempt == 3: raise
                sleep(2 ** (attempt + 1))
                continue
            current = api(endpoint)
            if name not in _verify(current, release, files):
                raise ValueError('uploaded asset readback required')
            break
    current = api(endpoint)
    if _verify(current, release, files) != set(files): raise ValueError('complete draft readback required')
    for attempt in range(4):
        try:
            api(endpoint, method='PATCH', body={'draft': False, 'make_latest': 'false'})
        except GitHubError as exc:
            current = api(endpoint)
            _verify(current, release, files, published=True)
            if current.get('draft') is False:
                if _verify(current, release, files, published=True) != set(files):
                    raise ValueError('complete published asset readback required')
                return current
            if not exc.retryable or attempt == 3: raise
            sleep(2 ** (attempt + 1))
            continue
        current = api(endpoint)
        if _verify(current, release, files, published=True) != set(files) or current.get('draft') is not False:
            raise ValueError('published release readback required')
        return current
    raise AssertionError('bounded publication attempt loop')
