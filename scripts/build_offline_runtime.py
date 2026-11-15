"""Build deterministic Core and Extra runtimes from their immutable code bases."""
import argparse
from hashlib import sha256
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from urllib.request import urlopen
import zipfile

if __package__:
    from .collection_runtime import MAX_ARCHIVE_BYTES, bind_runtime, extract_runtime
else:
    from collection_runtime import MAX_ARCHIVE_BYTES, bind_runtime, extract_runtime


SCRIPTS = Path(__file__).resolve().parent
BUILDS = {
    'core': {
        'base_tag': 'offline-core-runtime-v11-20261115',
        'base_sha256': '4460f55c1fb10ad443ed2c9f28688cd2ef945d24b4c65bd707ffcb86accb9091',
        'patch': 'offline-runtime-v12.patch',
        'helpers': ('offline_publication.py', 'offline_outbox.py'),
    },
    'extra': {
        'base_tag': 'extra-source-runtime-v2-20261113',
        'base_sha256': '43305cfb68485104132cec546b34660595061d4ba4bbc2424c2b13bfa41d6a14',
        'patch': 'extra-runtime-v3.patch',
        'helpers': (),
        'hosted_scripts': {'remote_extended_cycle.py', 'remote-extended-requirements.txt', 'remote-requirements.txt'},
    },
}


def build(base, output, *, stream='core'):
    spec = BUILDS[stream]
    base, output = Path(base), Path(output).resolve()
    if output.exists():
        raise ValueError('fresh runtime output required')
    output.parent.mkdir(parents=True, exist_ok=True)
    patch = SCRIPTS / spec['patch']
    modified = [line[6:] for line in patch.read_text(encoding='utf-8').splitlines() if line.startswith('--- a/')]
    with TemporaryDirectory(prefix='build-runtime-', dir=output.parent) as temporary:
        directory = Path(temporary).resolve()
        extract_runtime(base, directory, spec['base_sha256'])
        for name in modified:
            source = (directory / name).resolve()
            if not source.is_relative_to(directory):
                raise ValueError('patch source must stay inside runtime')
            source.write_text(source.read_text(encoding='utf-8'), encoding='utf-8', newline='\n')
        apply = ['git', '-c', 'core.autocrlf=false', 'apply']
        subprocess.run([*apply, '--check', str(patch)], cwd=directory, check=True)
        subprocess.run([*apply, str(patch)], cwd=directory, check=True)
        for helper in spec['helpers']:
            text = (SCRIPTS / helper).read_text(encoding='utf-8')
            (directory / 'scripts' / helper).write_text(text, encoding='utf-8', newline='\n')
        with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as target:
            files = sorted(path.relative_to(directory).as_posix() for path in directory.rglob('*') if path.is_file())
            hosted_scripts = spec.get('hosted_scripts')
            for name in files:
                if hosted_scripts is not None and name.startswith('scripts/') and name[8:] not in hosted_scripts:
                    continue
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 0
                info.compress_type = zipfile.ZIP_DEFLATED
                target.writestr(info, (directory / name).read_bytes())
    return sha256(output.read_bytes()).hexdigest()



def download_runtime(tag):
    url = 'https://github.com/do0405/Backtest-infra/releases/download/' + tag + '/runtime.zip'
    with urlopen(url, timeout=60) as response:
        content = response.read(MAX_ARCHIVE_BYTES)
    if not 0 < len(content) < MAX_ARCHIVE_BYTES:
        raise ValueError('bounded runtime download required')
    return content


def verify_published_runtime(pin):
    if sha256(download_runtime(pin['runtime_tag'])).hexdigest() != pin['runtime_sha256']:
        raise ValueError('published runtime asset must match the reviewed pin')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stream', choices=BUILDS, default='core')
    parser.add_argument('--base', type=Path, help='Use a local copy of the same pinned build input')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--verify-and-extract', type=Path)
    parser.add_argument('--verify-published', action='store_true')
    args = parser.parse_args()
    if args.base is not None:
        print(build(args.base, args.output, stream=args.stream))
    else:
        with TemporaryDirectory(prefix='runtime-base-') as temporary:
            base=Path(temporary)/'runtime.zip'
            base.write_bytes(download_runtime(BUILDS[args.stream]['base_tag']))
            print(build(base, args.output, stream=args.stream))
    if args.verify_and_extract is not None or args.verify_published:
        pin = bind_runtime({}, args.stream)
        if args.verify_and_extract is not None:
            extract_runtime(args.output, args.verify_and_extract, pin['runtime_sha256'])
        if args.verify_published:
            verify_published_runtime(pin)
