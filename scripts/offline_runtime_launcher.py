"""Run a hash-pinned isolated collector runtime without altering the KRX code."""
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
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
    if output:
        with Path(output).open('wb') as file:result=subprocess.run(['gh',*arguments],stdout=file,stderr=subprocess.PIPE)
    else:result=subprocess.run(['gh',*arguments],capture_output=True)
    if result.returncode:raise RuntimeError('runtime_github_operation_failed')
    return result.stdout


def main():
    config=runtime_binding(json.loads(os.environ['OFFLINE_CONFIGURATION']),Path(__file__).with_name('offline-runtime.json'))
    repo=os.environ['REPOSITORY']
    directory=Path(os.environ['RUNNER_TEMP'])/'offline-isolated-runtime';directory.mkdir()
    release=json.loads(github(['api',f"repos/{repo}/releases/tags/{config['runtime_tag']}"]))
    assets=[row for row in release.get('assets',[]) if row['name']=='runtime.zip']
    if release.get('draft') or len(assets)!=1 or not 0<assets[0]['size']<64*1024**2:raise ValueError('published bounded runtime required')
    archive=directory/'runtime.zip'
    github(['api','-H','Accept: application/octet-stream',f"repos/{repo}/releases/assets/{assets[0]['id']}"],output=archive)
    if sha256(archive.read_bytes()).hexdigest()!=config['runtime_sha256']:raise ValueError('approved runtime hash mismatch')
    runtime=directory/'source';runtime.mkdir()
    with zipfile.ZipFile(archive) as source:
        expanded=0
        for member in source.infolist():
            path=PurePosixPath(member.filename);expanded+=member.file_size
            if path.is_absolute() or '..' in path.parts or ':' in member.filename or '\\' in member.filename or stat.S_ISLNK(member.external_attr>>16):
                raise ValueError('unsafe runtime archive path')
        if expanded>256*1024**2:raise ValueError('runtime expansion bound exceeded')
        source.extractall(runtime)
    subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check',str(runtime)],check=True)
    result=subprocess.run([sys.executable,str(runtime/'scripts/offline_refresh.py'),'--work',str(directory/'capture'),
                           '--budget-seconds',os.environ.get('BUDGET_SECONDS','5400')])
    return result.returncode


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status':'isolated_runtime_failed','error_type':type(exc).__name__}));raise SystemExit(2)
