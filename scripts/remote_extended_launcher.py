"""Restore an exact public code-only runtime; private data are decrypted inside it."""
from hashlib import sha256
import json
import os
from pathlib import Path,PurePosixPath
import shutil
import stat
import subprocess
import sys
import zipfile

ARCHIVE_BASE_SHA256='94adf2ef27ae16bbf2806fb8ab09dee5a38f03986a880a19af57922c892a0dba'
ARCHIVE_SOURCE_SHA256='c286220e451c554e71203c9f9c088a88535f6cded84e2a777c1a723df06af78c'
CYCLE_SOURCE_SHA256='087edbab9013606918340518ebeb4d12d837c8a2e86770672c5f475a578d8956'
ARCHIVE_PATCH_SHA256='581ad6cd155cef8379e658eaafaa0a3924159c336cdaada1d13e7d5c8c338eec'


def command(arguments,**kwargs):
    result=subprocess.run(arguments,capture_output=True,timeout=kwargs.pop('timeout',300),**kwargs)
    if result.returncode:raise RuntimeError('extra_runtime_operation_failed')
    return result.stdout


def apply_task_archive_overlay(runtime,base_sha256):
    if base_sha256!=ARCHIVE_BASE_SHA256:return
    patch=Path(__file__).with_name('extra-task-archive-v9.patch').resolve()
    if sha256(patch.read_bytes()).hexdigest()!=ARCHIVE_PATCH_SHA256:
        raise ValueError('exact extra runtime patch required')
    sources={
        runtime/'src/backtest_data/remote/task_archive.py':ARCHIVE_SOURCE_SHA256,
        runtime/'scripts/remote_extended_cycle.py':CYCLE_SOURCE_SHA256,
        runtime/'scripts/extra_us_coverage_audit.py':'6d869367fb4cf3be88c55862c7f10fe6ee86e0935991fb3a0cc1b4fff6bae394',
        runtime/'src/backtest_data/remote/extra_cadence.py':'6eb2f5eb633546792c06b809aeb1d7b35e315809c20dcf296cc08a6183945346',
        runtime/'src/backtest_data/remote/extra_price_reconcile.py':'2e2e1715b0c970f5bcf246584bf3df8ea1f0c297dc40b50e73f88aeda06e48f7',
        runtime/'src/backtest_data/remote/extra_price_mapping.py':'d4046968591c1824f4ca14d79f69a82c2da197d4e6079ee180f9f67a8eaab4bc',
        runtime/'src/backtest_data/remote/worker.py':'db0a7e1c815bb13a8267e4b6c524d4ca774ff506b615318857d8321061b0f985',
        runtime/'src/backtest_data/ingestion/jobs.py':'44c4a2eb149880c3271bc136c7f2c43d4622a26b9e0dfbee53cc528c40c6944a',
        runtime/'src/backtest_data/prices/storage.py':'a4cd5d85066646bdf7377ba3654193bd738391d8d47a737367754e20f3b9eece',
        runtime/'src/backtest_data/prices/kis_overseas_storage.py':'daff43d39bb2fc62060f3340f961399ce1f120f4d199e44a32c81a86a7c6c68a',
        runtime/'src/backtest_data/prices/adapters/kis_overseas.py':'204f3ee621f320d43bda6912849a552145f41d295aa2c58f81b9b8092a0b1dac',
        runtime/'src/backtest_data/remote/checkpoint.py':'3ac332101c85db1dba0bdbb0a990dbbe36145b309c6ecd57e0c7f825fb4fde57',
        runtime/'src/backtest_data/ingestion/supervisor.py':'9bd31d3288b85231cb8c07c0ab0210dba563986304b63cac1aa2bd680f6e4ae3',
    }
    for source in sources:
        if source.name in {'extra_price_reconcile.py', 'extra_price_mapping.py', 'extra_us_coverage_audit.py'} and not source.exists():
            continue
        canonical=source.read_text(encoding='utf-8')
        source.write_text(canonical,encoding='utf-8',newline='\n')
    arguments=['git','-c','core.autocrlf=false','apply']
    command([*arguments,'--check',str(patch)],cwd=runtime)
    command([*arguments,str(patch)],cwd=runtime)
    if any(sha256(source.read_bytes()).hexdigest()!=expected for source,expected in sources.items()):
        raise ValueError('exact extra runtime source hashes required')
    print(json.dumps({'status':'extra_task_archive_overlay_applied',
                      'base_runtime_sha256':base_sha256,'archive_source_sha256':ARCHIVE_SOURCE_SHA256,
                      'cycle_source_sha256':CYCLE_SOURCE_SHA256,'patch_sha256':ARCHIVE_PATCH_SHA256}),flush=True)


def main():
    config=json.loads(os.environ['REMOTE_EXTENDED_OWNERSHIP']);repo=os.environ['REPOSITORY']
    root=Path(os.environ['RUNNER_TEMP'])/'extra-isolated-runtime';root.mkdir()
    real_gh=shutil.which('gh')
    if real_gh is None:raise RuntimeError('GitHub CLI required')
    bin_dir=root/'bin';bin_dir.mkdir()
    gh=bin_dir/'gh'
    shutil.copyfile(Path(__file__).with_name('retry_release_asset.py'),gh)
    gh.chmod(0o700)
    env={**os.environ,'BACKTEST_REAL_GH':real_gh,'PATH':str(bin_dir)+os.pathsep+os.environ['PATH']}
    release=json.loads(command(['gh','api',f"repos/{repo}/releases/tags/{config['runtime_tag']}"]))
    assets=[row for row in release.get('assets',[]) if row['name']=='runtime.zip']
    if release.get('draft') or len(assets)!=1 or not 0<assets[0]['size']<64*1024**2:raise ValueError('bounded committed extra runtime required')
    archive=root/'runtime.zip'
    with archive.open('xb') as output:
        result=subprocess.run(['gh','api','-H','Accept: application/octet-stream',f"repos/{repo}/releases/assets/{assets[0]['id']}"],
            stdout=output,env=env,timeout=300)
    if result.returncode or sha256(archive.read_bytes()).hexdigest()!=config['runtime_sha256']:raise ValueError('exact approved extra runtime hash required')
    runtime=root/'source';runtime.mkdir()
    with zipfile.ZipFile(archive) as source:
        expanded=0
        if len(source.infolist())>10000:raise ValueError('bounded code-only runtime required')
        for member in source.infolist():
            path=PurePosixPath(member.filename);expanded+=member.file_size
            if path.is_absolute() or '..' in path.parts or ':' in member.filename or '\\' in member.filename or stat.S_ISLNK(member.external_attr>>16):
                raise ValueError('unsafe extra runtime path')
            if not (member.filename.startswith(('src/','scripts/','alembic/')) or member.filename in {'pyproject.toml','alembic.ini'}):
                raise ValueError('code-only extra runtime required')
        if expanded>256*1024**2:raise ValueError('bounded expanded extra runtime required')
        source.extractall(runtime)
    apply_task_archive_overlay(runtime,config['runtime_sha256'])
    command([sys.executable,'-m','pip','install','--no-deps','-r',str(runtime/'scripts/remote-extended-requirements.txt')])
    command([sys.executable,'-m','pip','install','--no-deps','--no-build-isolation','-e',str(runtime)])
    command([sys.executable,'-m','pip','check'])
    command([sys.executable,'-m','alembic','upgrade','head'],cwd=runtime)
    arguments = [sys.executable, str(runtime/'scripts/remote_extended_cycle.py'), '--work', str(root/'capture'),
                 '--budget-seconds', os.environ.get('BUDGET_SECONDS', '4800')]
    if audit_sequence := os.environ.get('EXTRA_AUDIT_PREVIOUS_SEQUENCE'):
        arguments.extend(('--audit-previous-sequence', audit_sequence))
    return subprocess.run(arguments,cwd=runtime,env=env).returncode


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:print(json.dumps({'status':'extra_runtime_failed','error_type':type(exc).__name__}));raise SystemExit(2)
