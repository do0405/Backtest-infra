"""Restore an exact public code-only runtime; private data are decrypted inside it."""
from hashlib import sha256
import json
import os
from pathlib import Path,PurePosixPath
import stat
import subprocess
import sys
import zipfile


def command(arguments,**kwargs):
    result=subprocess.run(arguments,capture_output=True,timeout=kwargs.pop('timeout',300),**kwargs)
    if result.returncode:raise RuntimeError('extra_runtime_operation_failed')
    return result.stdout


def main():
    config=json.loads(os.environ['REMOTE_EXTENDED_OWNERSHIP']);repo=os.environ['REPOSITORY']
    root=Path(os.environ['RUNNER_TEMP'])/'extra-isolated-runtime';root.mkdir()
    release=json.loads(command(['gh','api',f"repos/{repo}/releases/tags/{config['runtime_tag']}"]))
    assets=[row for row in release.get('assets',[]) if row['name']=='runtime.zip']
    if release.get('draft') or len(assets)!=1 or not 0<assets[0]['size']<64*1024**2:raise ValueError('bounded committed extra runtime required')
    archive=root/'runtime.zip'
    with archive.open('xb') as output:
        result=subprocess.run(['gh','api','-H','Accept: application/octet-stream',f"repos/{repo}/releases/assets/{assets[0]['id']}"],
            stdout=output,stderr=subprocess.PIPE,timeout=300)
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
    command([sys.executable,'-m','pip','install','--no-deps','-r',str(runtime/'scripts/remote-extended-requirements.txt')])
    command([sys.executable,'-m','pip','install','--no-deps','--no-build-isolation','-e',str(runtime)])
    command([sys.executable,'-m','pip','check'])
    command([sys.executable,'-m','alembic','upgrade','head'],cwd=runtime)
    return subprocess.run([sys.executable,str(runtime/'scripts/remote_extended_cycle.py'),'--work',str(root/'capture'),
        '--budget-seconds',os.environ.get('BUDGET_SECONDS','4800')],cwd=runtime).returncode


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:print(json.dumps({'status':'extra_runtime_failed','error_type':type(exc).__name__}));raise SystemExit(2)
