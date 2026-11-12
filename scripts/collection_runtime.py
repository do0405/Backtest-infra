"""The reviewed runtime pin and shared code-only archive boundary."""
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile


MAX_ARCHIVE_BYTES = 64 * 1024**2
MAX_EXPANDED_BYTES = 256 * 1024**2
PINS = {
    'core': ('offline-runtime.json', 'offline_runtime_pin_v1', r'offline-core-runtime-v\d+-\d{8}'),
    'extra': ('extra-runtime.json', 'extra_runtime_pin_v1', r'extra-source-runtime-v\d+-\d{8}'),
}


def pin_path(stream):
    return Path(__file__).with_name(PINS[stream][0])


def bind_runtime(configuration, stream, path=None):
    _, pin_format, tag_pattern = PINS[stream]
    pin = json.loads(Path(path or pin_path(stream)).read_text(encoding='utf-8'))
    if (not isinstance(pin, dict)
            or set(pin) != {'format', 'runtime_tag', 'runtime_sha256'}
            or pin['format'] != pin_format
            or not isinstance(pin['runtime_tag'], str)
            or not re.fullmatch(tag_pattern, pin['runtime_tag'])
            or not isinstance(pin['runtime_sha256'], str)
            or not re.fullmatch(r'[a-f0-9]{64}', pin['runtime_sha256'])):
        raise ValueError('complete approved runtime binding required')
    return {**configuration, 'runtime_tag': pin['runtime_tag'], 'runtime_sha256': pin['runtime_sha256']}


def extract_runtime(archive, destination, expected_sha256):
    archive = Path(archive)
    if not 0 < archive.stat().st_size < MAX_ARCHIVE_BYTES or sha256(archive.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError('approved runtime hash mismatch')
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if len(entries) > 10_000 or len({entry.filename for entry in entries}) != len(entries):
            raise ValueError('bounded unique runtime paths required')
        if sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
            raise ValueError('bounded runtime expansion required')
        for entry in entries:
            name = entry.filename
            path = PurePosixPath(name)
            if (not path.parts or path.is_absolute() or '..' in path.parts or ':' in name
                    or chr(92) in name or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError('unsafe runtime archive path')
            if path.parts[0] not in {'src', 'scripts', 'alembic'} and name not in {'pyproject.toml', 'alembic.ini'}:
                raise ValueError('code-only runtime required')
        Path(destination).mkdir(parents=True, exist_ok=True)
        source.extractall(destination)
