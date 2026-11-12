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
        'base_sha256': '59cb5541781ec62dd84004b717bd6f4d52fe09135d4d9e210fb058cc8440784f',
        'patch': 'offline-runtime-v7.patch',
        'helpers': ('offline_publication.py', 'offline_outbox.py'),
    },
    'extra': {
        'base_sha256': '94adf2ef27ae16bbf2806fb8ab09dee5a38f03986a880a19af57922c892a0dba',
        'patch': 'extra-task-archive-v9.patch',
        'helpers': (),
        'hosted_scripts': {'remote_extended_cycle.py', 'remote-extended-requirements.txt'},
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



def verify_published_runtime(pin):
    url = 'https://github.com/do0405/Backtest-infra/releases/download/' + pin['runtime_tag'] + '/runtime.zip'
    with urlopen(url, timeout=60) as response:
        content = response.read(MAX_ARCHIVE_BYTES)
    if not 0 < len(content) < MAX_ARCHIVE_BYTES or sha256(content).hexdigest() != pin['runtime_sha256']:
        raise ValueError('published runtime asset must match the reviewed pin')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stream', choices=BUILDS, default='core')
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--verify-and-extract', type=Path)
    parser.add_argument('--verify-published', action='store_true')
    args = parser.parse_args()
    print(build(args.base, args.output, stream=args.stream))
    if args.verify_and_extract is not None or args.verify_published:
        pin = bind_runtime({}, args.stream)
        if args.verify_and_extract is not None:
            extract_runtime(args.output, args.verify_and_extract, pin['runtime_sha256'])
        if args.verify_published:
            verify_published_runtime(pin)
