"""Gzip NDJSON data followed by a bounded, hashed commit manifest."""
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import uuid
from .codec import dumps, loads, pack, unpack, reject_constant
from .registry import validate_row

MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ROWS = 100_000


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""): result.update(block)
    return result.hexdigest()


def safe_path(root, relative):
    if not isinstance(relative, str) or "\\" in relative or ":" in relative or "\x00" in relative:
        raise ValueError("unsafe bundle path")
    p = PurePosixPath(relative)
    if p.is_absolute() or any(x in ("", ".", "..") for x in relative.split("/")):
        raise ValueError("unsafe bundle path")
    base = Path(root).resolve()
    target = base.joinpath(*p.parts)
    if not target.resolve().is_relative_to(base) or target.is_symlink(): raise ValueError("unsafe bundle path")
    return target


def write_bundle(root, rows, *, stream_id, sequence, scope, created_at=None, checkpoint=None,
                 previous_bundle_id=None, raw_files=None, max_rows=MAX_ROWS):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", stream_id) or type(sequence) is not int or sequence < 1:
        raise ValueError("invalid independent stream identity")
    if scope.get("target_count") != 17758 or scope.get("output_start") != "2016-01-01" or scope.get("output_end") != "2026-10-07":
        raise ValueError("whole original target scope is required")
    created_at = created_at or datetime.now(timezone.utc)
    bundle_id = str(uuid.uuid4()); root = Path(root); root.mkdir(parents=True, exist_ok=True)
    pending = root / (".partial-" + bundle_id); pending.mkdir()
    output = pending / "rows.ndjson.gz"; count = expanded = 0
    with output.open("wb") as binary, gzip.GzipFile(fileobj=binary, mode="wb", filename="", mtime=0) as compressed:
        for row in rows:
            validate_row(row); encoded = (dumps(row) + "\n").encode("utf-8")
            count += 1; expanded += len(encoded)
            if count > max_rows or expanded > MAX_EXPANDED_BYTES: raise ValueError("bundle expansion bound exceeded")
            compressed.write(encoded)
    files = [{"path": "rows.ndjson.gz", "sha256": digest(output), "bytes": output.stat().st_size,
              "expanded_bytes": expanded, "row_count": count, "kind": "rows"}]
    for storage_key, source_path in (raw_files or {}).items():
        if not re.fullmatch(r"sha256/[a-f0-9]{2}/[a-f0-9]{64}\.gz", storage_key): raise ValueError("unsafe raw storage path")
        relative = "raw/" + storage_key; destination = safe_path(pending, relative); destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination)
        files.append({"path": relative, "sha256": digest(destination), "bytes": destination.stat().st_size,
                      "kind": "raw", "storage_key": storage_key})
    if sum(f["bytes"] for f in files) > MAX_BUNDLE_BYTES: raise ValueError("bundle compressed bound exceeded")
    manifest = {"format": "backtest_remote_delta_v1", "bundle_id": bundle_id, "stream_id": stream_id,
        "sequence": sequence, "previous_bundle_id": previous_bundle_id, "scope": scope,
        "created_at": pack(created_at), "schema_version": "c71302d6b910", "codec_version": 1,
        "files": files, "checkpoint": pack(checkpoint or {}), "coverage_complete": False}
    # Manifest uses ordinary JSON except explicitly tagged source clocks.
    json_text = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    loads(json_text)
    (pending / "manifest.json").write_text(json_text, encoding="utf-8")
    final = root / bundle_id; pending.rename(final)
    return final


def read_manifest(bundle, *, expected_manifest_hash=None):
    return _read_manifest(bundle, expected_manifest_hash=expected_manifest_hash)[0]


def _read_manifest(bundle, *, expected_manifest_hash=None):
    """Return validated metadata and the hash of those same immutable bytes."""
    path = safe_path(bundle, "manifest.json")
    if path.stat().st_size > 128 * 1024 * 1024: raise ValueError("manifest size bound exceeded")
    # Hash, structural checks and private/typed-state validation must use the
    # same bytes. A portable checkpoint can be large on a slow local disk.
    content = path.read_bytes()
    if len(content) > 128 * 1024 * 1024: raise ValueError("manifest size bound exceeded")
    manifest_hash = hashlib.sha256(content).hexdigest()
    if expected_manifest_hash and manifest_hash != expected_manifest_hash: raise ValueError("manifest hash mismatch")
    encoded = content.decode("utf-8")
    raw = json.loads(encoded, parse_constant=reject_constant)
    unpack(raw)
    if raw.get("format") != "backtest_remote_delta_v1" or raw.get("schema_version") != "c71302d6b910": raise ValueError("unsupported bundle version")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", raw.get("stream_id", "")) or type(raw.get("sequence")) is not int or raw["sequence"] < 1: raise ValueError("invalid stream sequence")
    try: uuid.UUID(raw["bundle_id"])
    except (ValueError, KeyError, TypeError): raise ValueError("invalid bundle identity") from None
    scope = raw.get("scope", {})
    if scope.get("target_count") != 17758 or scope.get("output_start") != "2016-01-01" or scope.get("output_end") != "2026-10-07": raise ValueError("whole original target scope required")
    files = raw.get("files", [])
    if not files or len(files) > 100_000 or len({f["path"] for f in files}) != len(files): raise ValueError("invalid bundle file manifest")
    if sum(f["bytes"] for f in files) > MAX_BUNDLE_BYTES: raise ValueError("bundle compressed bound exceeded")
    if sum(f.get("row_count", 0) for f in files) > MAX_ROWS: raise ValueError("bundle row bound exceeded")
    for entry in files:
        safe_path(bundle, entry["path"])
        if type(entry["bytes"]) is not int or entry["bytes"] < 0 or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"]): raise ValueError("invalid bundle file bound/hash")
        if entry["kind"] not in ("rows", "raw"): raise ValueError("unsupported bundle file kind")
        if entry["kind"] == "raw" and entry["path"] != "raw/" + entry.get("storage_key", ""): raise ValueError("unsafe raw file path")
    return raw, manifest_hash


def iter_bundle_rows(bundle, *, expected_manifest_hash=None):
    manifest = read_manifest(bundle, expected_manifest_hash=expected_manifest_hash)
    for entry in manifest["files"]:
        path = safe_path(bundle, entry["path"])
        if path.stat().st_size != entry["bytes"] or digest(path) != entry["sha256"]: raise ValueError("bundle payload hash mismatch")
    total = 0
    for entry in manifest["files"]:
        if entry["kind"] != "rows": continue
        count = expanded = 0
        with gzip.open(safe_path(bundle, entry["path"]), "rb") as stream:
            while True:
                line = stream.readline(8 * 1024 * 1024 + 1)
                if not line: break
                count += 1; total += 1; expanded += len(line)
                if len(line) > 8 * 1024 * 1024 or expanded > MAX_EXPANDED_BYTES or total > MAX_ROWS: raise ValueError("bundle expansion bound exceeded")
                row = loads(line); validate_row(row); yield row
        if count != entry["row_count"] or expanded != entry["expanded_bytes"]: raise ValueError("bundle row count/expansion mismatch")
