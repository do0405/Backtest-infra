"""A SQL snapshot and exactly its immutable price/directory evidence files."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from uuid import uuid4

from sqlalchemy import func, select, text

from ..db import Base, load_models, make_engine, utcnow
from ..ingestion.models import RawObject
from ..ingestion.raw_store import RawStore


def _hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _child(root, key):
    path = (root / key).resolve()
    if not path.is_relative_to(root.resolve()) or Path(key).is_absolute() or "\\" in key or ":" in key:
        raise ValueError("Backup path escapes its manifest directory")
    return path


def _docker(engine):
    # This backend has been tested with the workspace Compose PostgreSQL.
    # Remote databases need an explicitly configured native pg_dump backend.
    if engine.url.host not in {"localhost", "127.0.0.1"} or engine.url.port != 54328:
        raise ValueError("Backup backend requires the local Compose database")
    container = os.environ.get("BACKTEST_POSTGRES_CONTAINER", "backtest-infra-postgres-1")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", container):
        raise ValueError("Invalid database container")
    return ["docker", "exec", container], engine.url.username, engine.url.database


def _run(args, *, stdin=None, stdout=None):
    try:
        result = subprocess.run(args, stdin=stdin, stdout=stdout or subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False, timeout=3600)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("Database backup command unavailable or timed out") from None
    if result.returncode:
        raise ValueError("Database backup command failed (details redacted)")
    return result


def backup_dataset(engine, raw_root, *, destination=None):
    load_models()
    prefix, username, database = _docker(engine)
    root = Path(destination or (Path("data/backups") / (utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]))).resolve()
    root.mkdir(parents=True, exist_ok=False)
    dump = root / "database.dump"
    started = time.monotonic()
    source_store = RawStore(Path(raw_root))
    rows = []
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        with connection.begin():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            snapshot = connection.scalar(text("SELECT pg_export_snapshot()"))
            server_version = connection.scalar(text("SHOW server_version"))
            snapshot_at = connection.scalar(text("SELECT transaction_timestamp()"))
            counts = {table.name: connection.scalar(select(func.count()).select_from(table)) for table in Base.metadata.sorted_tables}
            rows = [dict(row) for row in connection.execute(select(RawObject.__table__)).mappings()]
            with dump.open("xb") as stream:
                _run(prefix + ["pg_dump", "-U", username, "-d", database, "--format=custom", "--no-owner", "--snapshot=" + snapshot], stdout=stream)
                stream.flush()
                os.fsync(stream.fileno())
            for row in rows:
                content = source_store.read(row["storage_key"])
                if len(content) != row["byte_count"] or hashlib.sha256(content).hexdigest() != row["content_hash"]:
                    raise ValueError("Raw evidence checksum differs from SQL snapshot")
                target = _child(root / "raw", row["storage_key"])
                target.parent.mkdir(parents=True, exist_ok=True)
                with source_store._path(row["storage_key"]).open("rb") as original, target.open("xb") as replica:
                    shutil.copyfileobj(original, replica)
                    replica.flush()
                    os.fsync(replica.fileno())
    manifest = {"format_version": "dataset_backup_v1", "snapshot_consistent": True,
        "snapshot_at": snapshot_at.isoformat(), "database": database, "server_version": server_version,
        "database_dump": dump.name, "dump_sha256": _hash_file(dump), "table_counts": counts,
        "raw_objects": [{key: row[key] for key in ("id", "content_hash", "storage_key", "byte_count")} for row in rows],
        "financial_documents_archived": False, "financial_recovery_input": "structured_SQL",
        "disaster_independent": False, "failure_domain": "workspace_copy_requires_external_replication",
        "global_roles_included": False, "pitr_enabled": False, "created_at": utcnow().isoformat()}
    temporary = root / "manifest.pending"
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    manifest_path = root / "manifest.json"
    temporary.replace(manifest_path)
    verify_backup(manifest_path)
    return {"status": "backup_verified", "backup_manifest": str(manifest_path), "snapshot_at": manifest["snapshot_at"],
            "raw_objects": len(rows), "dump_bytes": dump.stat().st_size, "elapsed_seconds": round(time.monotonic() - started, 2),
            "disaster_independent": False, "pitr_enabled": False}


def _read_manifest(path):
    if not path:
        raise ValueError("Backup manifest path required")
    path = Path(path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("format_version") != "dataset_backup_v1" or not manifest.get("snapshot_consistent"):
        raise ValueError("Unsupported or inconsistent backup manifest")
    return path, manifest


def verify_backup(path):
    path, manifest = _read_manifest(path)
    dump = _child(path.parent, manifest["database_dump"])
    if not dump.is_file() or _hash_file(dump) != manifest["dump_sha256"]:
        raise ValueError("Database dump checksum failure")
    store = RawStore(path.parent / "raw")
    for row in manifest["raw_objects"]:
        content = store.read(row["storage_key"])
        if len(content) != row["byte_count"] or hashlib.sha256(content).hexdigest() != row["content_hash"]:
            raise ValueError("Raw backup checksum failure")
    return {"status": "backup_verified", "integrity_verified": True, "raw_objects": len(manifest["raw_objects"]),
            "snapshot_consistent": True, "disaster_independent": manifest["disaster_independent"],
            "backup_manifest": str(path)}


def restore_drill(engine, path):
    """Restore into a newly generated disposable DB, never an existing database."""
    verify_backup(path)
    path, manifest = _read_manifest(path)
    load_models()
    prefix, username, _ = _docker(engine)
    target = "backtest_restore_" + uuid4().hex + "_test"
    started = time.monotonic()
    _run(prefix + ["createdb", "-U", username, target])
    restored_engine = None
    try:
        with _child(path.parent, manifest["database_dump"]).open("rb") as stream:
            _run(prefix[:1] + ["exec", "-i", prefix[-1], "pg_restore", "-U", username, "-d", target, "--no-owner", "--exit-on-error"], stdin=stream)
        restored_engine = make_engine(engine.url.set(database=target).render_as_string(hide_password=False))
        with restored_engine.connect() as connection:
            counts = {table.name: connection.scalar(select(func.count()).select_from(table)) for table in Base.metadata.sorted_tables}
            restored_raw = sorted(connection.execute(select(RawObject.content_hash)).scalars())
        if counts != manifest["table_counts"] or restored_raw != sorted(row["content_hash"] for row in manifest["raw_objects"]):
            raise ValueError("Restored SQL inventory differs from backup snapshot")
        return {"status": "restore_verified", "table_count": len(counts), "source_financial_facts": counts.get("source_financial_fact", 0),
                "price_observations": counts.get("price_observation", 0), "elapsed_seconds": round(time.monotonic() - started, 2),
                "restored_into_disposable_test_database": True, "disaster_independent": False, "pitr_enabled": False}
    finally:
        if restored_engine is not None:
            restored_engine.dispose()
        # This exact, newly created random database belongs only to this drill.
        _run(prefix + ["dropdb", "-U", username, "--if-exists", target])
