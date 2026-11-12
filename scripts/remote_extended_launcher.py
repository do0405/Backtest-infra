"""Restore an exact public code-only runtime; private data are decrypted inside it."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

if __package__:
    from .collection_runtime import MAX_ARCHIVE_BYTES, bind_runtime, extract_runtime
else:
    from collection_runtime import MAX_ARCHIVE_BYTES, bind_runtime, extract_runtime


def command(arguments,**kwargs):
    result=subprocess.run(arguments,capture_output=True,timeout=kwargs.pop('timeout',300),**kwargs)
    if result.returncode:raise RuntimeError('extra_runtime_operation_failed')
    return result.stdout


def main():
    if os.environ.get('EXTRA_AUDIT_PREVIOUS_SEQUENCE'):
        raise ValueError('historical audit is not supported by the production collector')
    config = bind_runtime(json.loads(os.environ['REMOTE_EXTENDED_OWNERSHIP']), 'extra')
    repo = os.environ['REPOSITORY']
    root=Path(os.environ['RUNNER_TEMP'])/'extra-isolated-runtime';root.mkdir()
    real_gh=shutil.which('gh')
    if real_gh is None:raise RuntimeError('GitHub CLI required')
    bin_dir=root/'bin';bin_dir.mkdir()
    gh=bin_dir/'gh'
    shutil.copyfile(Path(__file__).with_name('retry_release_asset.py'),gh)
    gh.chmod(0o700)
    env = {
        **os.environ,
        'REMOTE_EXTENDED_OWNERSHIP': json.dumps(config),
        'BACKTEST_REAL_GH': real_gh,
        'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
    }
    release=json.loads(command(['gh','api',f"repos/{repo}/releases/tags/{config['runtime_tag']}"]))
    assets=[row for row in release.get('assets',[]) if row['name']=='runtime.zip']
    if release.get('draft') or len(assets)!=1 or not 0<assets[0]['size']<MAX_ARCHIVE_BYTES:raise ValueError('bounded committed extra runtime required')
    archive=root/'runtime.zip'
    with archive.open('xb') as output:
        result=subprocess.run(['gh','api','-H','Accept: application/octet-stream',f"repos/{repo}/releases/assets/{assets[0]['id']}"],
            stdout=output,env=env,timeout=300)
    if result.returncode:
        raise RuntimeError('extra_runtime_download_failed')
    runtime = root/'source'
    extract_runtime(archive, runtime, config['runtime_sha256'])
    command([sys.executable,'-m','pip','install','--no-deps','-r',str(runtime/'scripts/remote-extended-requirements.txt')])
    command([sys.executable,'-m','pip','install','--no-deps','--no-build-isolation','-e',str(runtime)])
    command([sys.executable,'-m','pip','check'])
    command([sys.executable,'-m','alembic','upgrade','head'],cwd=runtime)
    arguments = [sys.executable, str(runtime/'scripts/remote_extended_cycle.py'), '--work', str(root/'capture'),
                 '--budget-seconds', os.environ.get('BUDGET_SECONDS', '4800')]
    return subprocess.run(arguments,cwd=runtime,env=env).returncode


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:print(json.dumps({'status':'extra_runtime_failed','error_type':type(exc).__name__}));raise SystemExit(2)
