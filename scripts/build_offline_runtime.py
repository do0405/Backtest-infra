"""Rebuild runtime v6 from the immutable v5 source and reviewed source patch."""
import argparse
from hashlib import sha256
from pathlib import Path, PurePosixPath
import stat
import subprocess
from tempfile import TemporaryDirectory
import zipfile

BASE_SHA256 = '3de6f41572962f9c3b2270ecd7f96fce7d09dda397fadf9ab94fa06737c6799b'
CHANGED = {'scripts/offline_refresh.py', 'src/backtest_data/offline/transport.py',
           'src/backtest_data/remote/releases.py'}


def build(base, output):
    base, output = Path(base), Path(output).resolve()
    raw = base.read_bytes()
    if sha256(raw).hexdigest() != BASE_SHA256: raise ValueError('immutable v5 runtime hash required')
    patch = Path(__file__).with_name('offline-runtime-v6.patch')
    names = {line[6:] for line in patch.read_text(encoding='utf-8').splitlines()
             if line.startswith(('--- a/', '+++ b/'))}
    if names != CHANGED: raise ValueError('exact runtime patch scope required')
    if output.exists(): raise ValueError('fresh runtime output required')
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='build-runtime-', dir=output.parent) as temporary:
        directory = Path(temporary).resolve()
        if not directory.is_relative_to(output.parent): raise ValueError('workspace build directory required')
        with zipfile.ZipFile(base) as source:
            entries = source.infolist()
            if len({item.filename for item in entries}) != len(entries): raise ValueError('unique runtime paths required')
            for item in entries:
                path = PurePosixPath(item.filename)
                if (path.is_absolute() or '..' in path.parts or ':' in item.filename or '\\' in item.filename or
                        stat.S_ISLNK(item.external_attr >> 16)):
                    raise ValueError('safe runtime archive required')
            if sum(item.file_size for item in entries) > 256*1024**2: raise ValueError('bounded runtime required')
            source.extractall(directory)
        subprocess.run(['git', 'apply', '--check', str(patch.resolve())], cwd=directory, check=True)
        subprocess.run(['git', 'apply', str(patch.resolve())], cwd=directory, check=True)
        helper = Path(__file__).with_name('offline_publication.py').read_bytes()
        (directory/'scripts/offline_publication.py').write_bytes(helper)
        with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as target:
            for name in sorted([item.filename for item in entries] + ['scripts/offline_publication.py']):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                target.writestr(info, (directory/name).read_bytes())
    return sha256(output.read_bytes()).hexdigest()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    arguments = parser.parse_args()
    print(build(arguments.base, arguments.output))
