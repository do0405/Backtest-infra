from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import select


pytestmark = pytest.mark.integration
STAMP = datetime(2026, 10, 11, 4, 5, 6, 123456, tzinfo=timezone.utc)
AUTHORITY = "new_remote_ephemeral_database"


def original(content=b'{"OutBlock_1":[]}'):
    digest = hashlib.sha256(content).hexdigest()
    return {"id": str(uuid4()), "content_hash": digest,
            "storage_key": f"sha256/{digest[:2]}/{digest}.gz", "byte_count": len(content), "created_at": STAMP}


def portable(*values):
    return {"version": 1, "rows": list(values)}


def test_metadata_restore_and_repeat_hash_preserve_original_uuid_clock_without_old_bytes(db_session, tmp_path):
    from backtest_data.remote.identity import restore_raw_identities
    from backtest_data.ingestion.models import RawObject
    from backtest_data.ingestion.http import ProviderResponse
    from backtest_data.prices.krx_quote_storage import store_krx_quotes
    from backtest_data.ingestion.raw_store import RawStore
    content = b'{"OutBlock_1":[]}'
    value = original(content)
    assert restore_raw_identities(db_session, portable(value), authority=AUTHORITY)["inserted_rows"] == 1
    assert not tmp_path.joinpath(value["storage_key"]).exists()
    response = ProviderResponse(content, 200, STAMP + timedelta(days=1), "application/json")
    result = store_krx_quotes(db_session, "kospi", date(2026, 10, 7), response, json.loads(content), tmp_path)
    assert result["raw_id"] == value["id"]
    assert db_session.get(RawObject, value["id"]).created_at == STAMP
    assert RawStore(tmp_path).read(value["storage_key"]) == content


def test_catalog_existing_identity_is_exact_idempotent_and_no_updates(db_session):
    from backtest_data.remote.identity import restore_raw_identities, snapshot_raw_identities
    from backtest_data.ingestion.models import RawObject
    value = original(str(uuid4()).encode())
    db_session.add(RawObject(**value)); db_session.flush()
    first = restore_raw_identities(db_session, portable(value), authority=AUTHORITY)
    second = restore_raw_identities(db_session, portable(value), authority=AUTHORITY)
    assert first == second == {"inserted_rows": 0, "existing_rows": 1, "total_rows": 1}
    exported = snapshot_raw_identities(db_session)
    assert value in exported["rows"]


@pytest.mark.parametrize("conflict", ["id", "hash", "metadata"])
def test_conflicting_identity_rolls_back_all_new_rows_and_preserves_existing(db_session, conflict, monkeypatch):
    from backtest_data.remote import identity
    restore_raw_identities = identity.restore_raw_identities
    monkeypatch.setattr(identity, '_BATCH_SIZE', 1)
    from backtest_data.ingestion.models import RawObject
    existing = original(str(uuid4()).encode())
    db_session.add(RawObject(**existing)); db_session.flush()
    fresh = original(str(uuid4()).encode())
    if conflict == "id":
        incoming = original(str(uuid4()).encode()) | {"id": existing["id"]}
    elif conflict == "hash":
        incoming = existing | {"id": str(uuid4())}
    else:
        incoming = existing | {"created_at": STAMP + timedelta(microseconds=1)}
    with pytest.raises(ValueError, match="collision"):
        restore_raw_identities(db_session, portable(fresh, incoming), authority=AUTHORITY)
    assert db_session.get(RawObject, fresh["id"]) is None
    preserved = db_session.get(RawObject, existing["id"])
    assert preserved.content_hash == existing["content_hash"] and preserved.created_at == STAMP
    assert db_session.scalar(select(RawObject.id).where(RawObject.content_hash == fresh["content_hash"])) is None


def test_wrong_authority_or_malformed_metadata_do_not_write(db_session):
    from backtest_data.remote.identity import restore_raw_identities
    from backtest_data.ingestion.models import RawObject
    value = original(str(uuid4()).encode())
    with pytest.raises(ValueError, match="authority"):
        restore_raw_identities(db_session, portable(value), authority="existing_local_database")
    with pytest.raises(ValueError):
        restore_raw_identities(db_session, portable(value | {"byte_count": -1}), authority=AUTHORITY)
    assert db_session.get(RawObject, value["id"]) is None


def test_generic_bundle_import_still_rejects_different_id_for_same_raw_hash(db_session, tmp_path):
    from backtest_data.remote.identity import restore_raw_identities
    from backtest_data.remote.bundle import write_bundle
    from backtest_data.remote.sql import import_bundle
    from backtest_data.ingestion.raw_store import RawStore
    content = str(uuid4()).encode()
    value = original(content)
    restore_raw_identities(db_session, portable(value), authority=AUTHORITY)
    stored = RawStore(tmp_path / "producer").put(content)
    different = value | {"id": str(uuid4())}
    scope = {"target_count": 17758, "output_start": "2016-01-01", "output_end": "2026-10-07"}
    bundle = write_bundle(tmp_path / "bundles", [{"table": "raw_object", "values": different}],
                          stream_id="identity-test-" + uuid4().hex, sequence=1, scope=scope,
                          raw_files={stored.storage_key: tmp_path / "producer" / stored.storage_key})
    with pytest.raises(ValueError, match="natural key"):
        import_bundle(db_session, bundle, raw_root=tmp_path / "local")
