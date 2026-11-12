"""Run a hash-pinned isolated collector runtime without altering the KRX code."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


if __package__:
    from .collection_runtime import MAX_ARCHIVE_BYTES, bind_runtime, extract_runtime
else:
    from collection_runtime import MAX_ARCHIVE_BYTES, bind_runtime, extract_runtime


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


def main():
    config = bind_runtime(json.loads(os.environ['OFFLINE_CONFIGURATION']), 'core')
    repo=os.environ['REPOSITORY']
    directory=Path(os.environ['RUNNER_TEMP'])/'offline-isolated-runtime';directory.mkdir()
    release=json.loads(github(['api',f"repos/{repo}/releases/tags/{config['runtime_tag']}"]))
    assets=[row for row in release.get('assets',[]) if row['name']=='runtime.zip']
    if release.get('draft') or len(assets)!=1 or not 0<assets[0]['size']<MAX_ARCHIVE_BYTES:raise ValueError('published bounded runtime required')
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
