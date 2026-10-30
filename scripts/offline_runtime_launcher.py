"""Run a hash-pinned isolated collector runtime without altering the KRX code."""
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import time
import zipfile


def runtime_binding(configuration, path):
    """A reviewed public pin changes only the runtime, preserving private gates."""
    if not Path(path).exists(): return configuration
    pin = json.loads(Path(path).read_text(encoding='utf-8'))
    if (set(pin) != {'format', 'runtime_tag', 'runtime_sha256'} or
            pin['format'] != 'offline_runtime_pin_v1' or
            not re.fullmatch(r'offline-core-runtime-v\d+-\d{8}', pin['runtime_tag']) or
            not re.fullmatch(r'[a-f0-9]{64}', pin['runtime_sha256'])):
        raise ValueError('complete approved runtime binding required')
    return {**configuration, 'runtime_tag': pin['runtime_tag'], 'runtime_sha256': pin['runtime_sha256']}


def github(arguments, output=None):
    from offline_publication import GitHubError, network_failure
    for attempt in range(4):
        try:
            if output:
                with Path(output).open('wb') as file:
                    result=subprocess.run(['gh',*arguments],stdout=file,stderr=subprocess.PIPE,timeout=120)
            else:result=subprocess.run(['gh',*arguments],capture_output=True,timeout=120)
        except (FileNotFoundError,PermissionError) as exc:
            raise ValueError('github_cli_unavailable') from exc
        except (subprocess.TimeoutExpired,OSError):
            error=GitHubError(None,'runtime_bootstrap',transient=True)
        else:
            if not result.returncode:return result.stdout
            match=re.search(rb'HTTP(?:/[\d.]+)?\s+(\d{3})',result.stderr or b'')
            status=int(match[1]) if match else None
            error=GitHubError(status,'runtime_bootstrap',rate_limited=status==403 and b'rate limit' in (result.stderr or b'').lower(),
                              transient=status is None and network_failure(result.stderr))
        if not error.retryable or attempt==3:raise error
        time.sleep(2**(attempt+1))


def extract_runtime(archive, runtime, expected_sha256):
    archive, runtime = Path(archive), Path(runtime)
    if not 0 < archive.stat().st_size < 64*1024**2 or sha256(archive.read_bytes()).hexdigest()!=expected_sha256:
        raise ValueError('approved runtime hash mismatch')
    runtime.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        entries=source.infolist()
        if len({member.filename for member in entries})!=len(entries):raise ValueError('unique runtime paths required')
        expanded=0
        for member in entries:
            path=PurePosixPath(member.filename);expanded+=member.file_size
            if path.is_absolute() or '..' in path.parts or ':' in member.filename or '\\' in member.filename or stat.S_ISLNK(member.external_attr>>16):
                raise ValueError('unsafe runtime archive path')
        if expanded>256*1024**2:raise ValueError('runtime expansion bound exceeded')
        source.extractall(runtime)


def main():
    config=runtime_binding(json.loads(os.environ['OFFLINE_CONFIGURATION']),Path(__file__).with_name('offline-runtime.json'))
    repo=os.environ['REPOSITORY']
    directory=Path(os.environ['RUNNER_TEMP'])/'offline-isolated-runtime';directory.mkdir()
    release=json.loads(github(['api',f"repos/{repo}/releases/tags/{config['runtime_tag']}"]))
    assets=[row for row in release.get('assets',[]) if row['name']=='runtime.zip']
    if release.get('draft') or len(assets)!=1 or not 0<assets[0]['size']<64*1024**2:raise ValueError('published bounded runtime required')
    archive=directory/'runtime.zip'
    github(['api','-H','Accept: application/octet-stream',f"repos/{repo}/releases/assets/{assets[0]['id']}"],output=archive)
    runtime=directory/'source'
    extract_runtime(archive,runtime,config['runtime_sha256'])
    subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check',str(runtime)],check=True)
    result=subprocess.run([sys.executable,str(runtime/'scripts/offline_refresh.py'),'--work',str(directory/'capture'),
                           '--budget-seconds',os.environ.get('BUDGET_SECONDS','5400')])
    return result.returncode


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        from offline_publication import output_error
        output_error(exc,'isolated_runtime_failed')
        raise SystemExit(0 if getattr(exc,'retryable',False) else 2)
