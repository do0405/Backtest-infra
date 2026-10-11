from dataclasses import dataclass
import gzip
import hashlib
import os
from pathlib import Path, PurePosixPath
import tempfile


@dataclass(frozen=True)
class StoredRaw:
    content_hash: str
    storage_key: str
    byte_count: int


class RawStore:
    """Immutable content-addressed storage for price/directory evidence only."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        pieces = PurePosixPath(key)
        if pieces.is_absolute() or ".." in pieces.parts or "\\" in key or ":" in key:
            raise ValueError("Invalid storage key")
        candidate = (self.root / key).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("Invalid storage key")
        return candidate

    def put(self, content: bytes) -> StoredRaw:
        digest = hashlib.sha256(content).hexdigest()
        key = f"sha256/{digest[:2]}/{digest}.gz"
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if self.read(key) != content:
                raise ValueError("Archive integrity failure")
        else:
            fd, temporary = tempfile.mkstemp(dir=target.parent, prefix="pending-")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(gzip.compress(content, mtime=0))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if Path(temporary).exists():
                    Path(temporary).unlink()
        return StoredRaw(digest, key, len(content))

    def read(self, storage_key: str) -> bytes:
        path = self._path(storage_key)
        try:
            content = gzip.decompress(path.read_bytes())
        except (OSError, EOFError) as exc:
            raise ValueError("Archive integrity failure") from None
        if hashlib.sha256(content).hexdigest() != path.name.removesuffix(".gz"):
            raise ValueError("Archive integrity failure")
        return content
