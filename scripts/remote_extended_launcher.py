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


class LaunchError(RuntimeError):
    def __init__(self, stage, reason, returncode=None):
        self.stage, self.reason, self.returncode = stage, reason, returncode
        super().__init__(f'{stage}: {reason}')


def command(stage, arguments, **kwargs):
    try:
        result = subprocess.run(arguments, capture_output=True, timeout=300, **kwargs)
    except subprocess.TimeoutExpired:
        raise LaunchError(stage, 'timeout') from None
    if result.returncode:
        raise LaunchError(stage, 'command_failed', result.returncode)
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
    release=json.loads(command('release_lookup', ['gh','api',f"repos/{repo}/releases/tags/{config['runtime_tag']}"]))
    assets=[row for row in release.get('assets',[]) if row['name']=='runtime.zip']
    if release.get('draft') or len(assets)!=1 or not 0<assets[0]['size']<MAX_ARCHIVE_BYTES:raise ValueError('bounded committed extra runtime required')
    archive=root/'runtime.zip'
    with archive.open('xb') as output:
        result=subprocess.run(['gh','api','-H','Accept: application/octet-stream',f"repos/{repo}/releases/assets/{assets[0]['id']}"],
            stdout=output,env=env,timeout=300)
    if result.returncode:
        raise LaunchError('runtime_download', 'command_failed', result.returncode)
    runtime = root/'source'
    extract_runtime(archive, runtime, config['runtime_sha256'])
    command('install_dependencies', [sys.executable,'-m','pip','install','--no-deps','-r',str(runtime/'scripts/remote-extended-requirements.txt')])
    command('install_runtime', [sys.executable,'-m','pip','install','--no-deps','--no-build-isolation','-e',str(runtime)])
    command('check_dependencies', [sys.executable,'-m','pip','check'])
    command('migrate_database', [sys.executable,'-m','alembic','upgrade','head'],cwd=runtime)
    arguments = [sys.executable, str(runtime/'scripts/remote_extended_cycle.py'), '--work', str(root/'capture'),
                 '--budget-seconds', os.environ.get('BUDGET_SECONDS', '4800')]
    return subprocess.run(arguments,cwd=runtime,env=env).returncode


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        report = {'status': 'extra_runtime_failed', 'error_type': type(exc).__name__}
        if isinstance(exc, LaunchError):
            report.update(stage=exc.stage, reason=exc.reason, returncode=exc.returncode)
        print(json.dumps(report))
        raise SystemExit(2)
