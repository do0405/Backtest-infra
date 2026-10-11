"""Original content identities for a fresh remote database, without old bytes.

The index is metadata rather than a new source observation. A later response
with identical bytes reuses its original raw UUID and creation clock. Generic
bundle import remains strict and never rewrites a local natural-key collision.
"""
from datetime import datetime
import re
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert

from ..ingestion.models import CollectionJob, RawObject
from .codec import dumps


AUTHORITY = "new_remote_ephemeral_database"
MAX_RAW_IDENTITY_ROWS = 500_000
MAX_RAW_IDENTITY_BYTES = 96 * 1024 * 1024
_COLUMNS = frozenset({"id", "content_hash", "storage_key", "byte_count", "created_at"})
_BATCH_SIZE = 500


def _bounds(max_rows, max_bytes):
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_RAW_IDENTITY_ROWS:
        raise ValueError("positive bounded raw identity row limit required")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_RAW_IDENTITY_BYTES:
        raise ValueError("positive bounded raw identity byte limit required")


def validate_raw_identity_index(index, *, max_rows=MAX_RAW_IDENTITY_ROWS, max_bytes=MAX_RAW_IDENTITY_BYTES):
    """Validate complete original columns, uniqueness, clocks and payload bounds."""
    _bounds(max_rows, max_bytes)
    if not isinstance(index, dict) or set(index) != {"version", "rows"} or type(index["version"]) is not int or index["version"] != 1:
        raise ValueError("versioned raw identity metadata required")
    rows = index["rows"]
    if not isinstance(rows, list):
        raise ValueError("raw identity rows must be a bounded list")
    if len(rows) > max_rows:
        raise ValueError("raw identity row bound exceeded")
    ids, hashes, keys = set(), set(), set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != _COLUMNS:
            raise ValueError("complete original raw identity columns required")
        identity, digest = row["id"], row["content_hash"]
        try:
            if not isinstance(identity, str) or str(UUID(identity)) != identity:
                raise ValueError("original canonical raw UUID required")
        except (ValueError, AttributeError, TypeError):
            raise ValueError("original canonical raw UUID required") from None
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("original raw content hash required")
        if row["storage_key"] != f"sha256/{digest[:2]}/{digest}.gz":
            raise ValueError("raw storage key must match its content hash")
        if type(row["byte_count"]) is not int or not 0 <= row["byte_count"] <= 2**31 - 1:
            raise ValueError("original nonnegative raw byte count required")
        clock = row["created_at"]
        if not isinstance(clock, datetime) or clock.tzinfo is None or clock.utcoffset() is None:
            raise ValueError("aware original raw creation clock required")
        if identity in ids or digest in hashes or row["storage_key"] in keys:
            raise ValueError("duplicate raw identity natural key")
        ids.add(identity); hashes.add(digest); keys.add(row["storage_key"])
    if len(dumps(index).encode("utf-8")) > max_bytes:
        raise ValueError("raw identity byte bound exceeded")
    return {"version": 1, "rows": [dict(row) for row in rows]}


def snapshot_raw_identities(session, *, max_rows=MAX_RAW_IDENTITY_ROWS, max_bytes=MAX_RAW_IDENTITY_BYTES):
    """Read each metadata row once and canonicalize UUID order in memory.

    UUID index order scatters reads across the live external-disk heap. SQL
    order is unnecessary for this bounded snapshot; final wire order is stable.
    """
    _bounds(max_rows, max_bytes)
    rows = []
    statement = select(*RawObject.__table__.c).limit(max_rows + 1).execution_options(yield_per=_BATCH_SIZE)
    for row in session.execute(statement).mappings().yield_per(_BATCH_SIZE):
        if len(rows) >= max_rows:
            raise ValueError("raw identity row bound exceeded")
        rows.append(dict(row))
    return validate_raw_identity_index({"version": 1, "rows": sorted(rows,key=lambda row:row['id'])}, max_rows=max_rows, max_bytes=max_bytes)


def restore_raw_identities(session, index, *, authority, max_rows=MAX_RAW_IDENTITY_ROWS, max_bytes=MAX_RAW_IDENTITY_BYTES):
    """Seed only fresh operational SQL; accept exact catalog parents idempotently.

All batches share one savepoint. Any changed original UUID, natural key or
metadata rolls the entire index back. No filesystem bytes are read or created.
"""
    if authority != AUTHORITY:
        raise ValueError("explicit fresh remote identity authority required")
    rows = validate_raw_identity_index(index, max_rows=max_rows, max_bytes=max_bytes)["rows"]
    if session.scalar(select(CollectionJob.id).limit(1)) is not None:
        raise ValueError("raw identity restore requires an empty operational queue")
    inserted, existing_count = 0, 0
    with session.begin_nested():
        for offset in range(0, len(rows), _BATCH_SIZE):
            batch = rows[offset:offset + _BATCH_SIZE]
            condition = or_(RawObject.id.in_([row["id"] for row in batch]),
                            RawObject.content_hash.in_([row["content_hash"] for row in batch]),
                            RawObject.storage_key.in_([row["storage_key"] for row in batch]))
            existing = [dict(row) for row in session.execute(select(*RawObject.__table__.c).where(condition)).mappings()]
            by_id = {row["id"]: row for row in existing}
            by_hash = {row["content_hash"]: row for row in existing}
            by_key = {row["storage_key"]: row for row in existing}
            missing = []
            for row in batch:
                matches = (by_id.get(row["id"]), by_hash.get(row["content_hash"]), by_key.get(row["storage_key"]))
                if any(match is not None and match != row for match in matches):
                    raise ValueError("original raw identity collision")
                if any(match is not None for match in matches):
                    existing_count += 1
                else:
                    missing.append(row)
            if missing:
                ids = list(session.scalars(insert(RawObject).values(missing).on_conflict_do_nothing().returning(RawObject.id)))
                if len(ids) != len(missing):
                    raise ValueError("concurrent original raw identity collision")
                inserted += len(ids)
    return {"inserted_rows": inserted, "existing_rows": existing_count, "total_rows": len(rows)}
