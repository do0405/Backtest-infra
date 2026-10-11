from datetime import datetime, timezone
import copy
import importlib
import importlib.util
from uuid import uuid4

import pytest


STAMP = datetime(2026, 10, 11, 4, 5, 6, 123456, tzinfo=timezone.utc)


def api():
    assert importlib.util.find_spec("backtest_data.remote.identity") is not None, "Original raw identity handoff is required"
    return importlib.import_module("backtest_data.remote.identity")


def row(value="a", **changes):
    digest = value * 64
    return {"id": str(uuid4()), "content_hash": digest,
            "storage_key": f"sha256/{digest[:2]}/{digest}.gz", "byte_count": 20,
            "created_at": STAMP} | changes


def index(*rows):
    return {"version": 1, "rows": list(rows)}


def test_raw_identity_metadata_retains_original_uuid_and_microsecond_clock():
    module = api()
    from backtest_data.remote.codec import dumps, loads
    original = index(row())
    validated = module.validate_raw_identity_index(loads(dumps(original)))
    assert validated == original
    assert validated["rows"][0]["created_at"].microsecond == 123456
    assert set(validated["rows"][0]) == {"id", "content_hash", "storage_key", "byte_count", "created_at"}


@pytest.mark.parametrize("change", [
    {"byte_count": True}, {"byte_count": -1}, {"content_hash": "ABC"},
    {"storage_key": "../raw.gz"}, {"storage_key": "sha256/bb/" + "a" * 64 + ".gz"},
    {"created_at": STAMP.replace(tzinfo=None)}, {"created_at": STAMP.isoformat()},
    {"id": "not-a-uuid"}, {"unexpected": "forbidden"},
])
def test_raw_identity_index_rejects_malformed_or_non_original_columns(change):
    with pytest.raises(ValueError):
        api().validate_raw_identity_index(index(row(**change)))


def test_duplicate_hash_or_uuid_is_rejected_without_selecting_one():
    first = row()
    for second in (row(id=first["id"], value="b"), row()):
        with pytest.raises(ValueError, match="duplicate"):
            api().validate_raw_identity_index(index(first, second))


def test_raw_identity_list_and_encoded_payload_are_bounded():
    module = api()
    with pytest.raises(ValueError, match="row bound"):
        module.validate_raw_identity_index(index(row(), row(value="b")), max_rows=1)
    with pytest.raises(ValueError, match="byte bound"):
        module.validate_raw_identity_index(index(row()), max_bytes=20)
    with pytest.raises(ValueError):
        module.validate_raw_identity_index(index(), max_rows=True)
    with pytest.raises(ValueError):
        module.validate_raw_identity_index({"version": True, "rows": []})


def test_wrong_authority_never_touches_sql():
    class NoSQL:
        def execute(self, *_):
            pytest.fail("SQL must not run without fresh remote authority")
    with pytest.raises(ValueError, match="authority"):
        api().restore_raw_identities(NoSQL(), index(row()), authority="local")


def test_explicit_authority_cannot_seed_a_live_operational_queue():
    class LiveSQL:
        def scalar(self, statement):
            assert "collection_job" in str(statement)
            return "live-owned-job"
        def begin_nested(self):
            pytest.fail("A live queue must be rejected before any seed write")
    with pytest.raises(ValueError, match="empty operational queue"):
        api().restore_raw_identities(LiveSQL(), index(row()), authority="new_remote_ephemeral_database")


def test_snapshot_is_ordered_bounded_exact_metadata_only():
    records = [row(value="b"), row()]
    records.sort(key=lambda value: value["id"])
    class Result:
        def mappings(self):
            return self
        def yield_per(self, _):
            return iter(records)
    class Snapshot:
        def execute(self, statement):
            assert "raw_object" in str(statement)
            assert "ORDER BY" in str(statement)
            return Result()
    module = api()
    assert module.snapshot_raw_identities(Snapshot()) == index(*records)
    original = copy.deepcopy(records)
    with pytest.raises(ValueError, match="row bound"):
        module.snapshot_raw_identities(Snapshot(), max_rows=1)
    assert records == original


@pytest.mark.parametrize("source, expected_handler_budget, expected_core_budget", [
    ("krx", 540, None), ("alpaca", 200, 340),
])
def test_krx_only_cycle_uses_full_explicit_handler_window_while_core_keeps_reserved_share(
        tmp_path, monkeypatch, source, expected_handler_budget, expected_core_budget):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from backtest_data.remote import worker
    from backtest_data.ingestion import supervisor
    from backtest_data.remote.folder import FolderTransport
    calls = {}
    initial_index = index()
    updated_index = index(row())
    class Session:
        def get(self, *_):
            return SimpleNamespace(id="manifest", members=[{}] * 17758,
                                   scope={"output_start": "2016-01-01", "output_end": "2026-10-07"})
        def scalars(self, _):
            assert calls.get("restored") is True, "Identity restore must precede before-snapshot reads"
            return []
    @contextmanager
    def transaction(_):
        yield Session()
    def handlers(*_, **kwargs):
        calls["handler"] = kwargs["budget_seconds"]
        return {}
    def collect(*_, **kwargs):
        calls["core"] = kwargs["budget_seconds"]
        return {}
    def restored(*_, **kwargs):
        assert kwargs["authority"] == "new_remote_ephemeral_database"
        calls["restored"] = True
    def exported(*_, **kwargs):
        calls["durable_checkpoint"] = kwargs["checkpoint"]
        return [tmp_path / "bundle"]
    monkeypatch.setattr(worker, "transaction", transaction)
    monkeypatch.setattr(worker, "restore_tasks", lambda *_, **__: 0)
    monkeypatch.setattr(worker, "restore_raw_identities", restored)
    monkeypatch.setattr(worker, "snapshot_raw_identities", lambda *_: updated_index)
    monkeypatch.setattr(worker, "export_deltas", exported)
    monkeypatch.setattr(FolderTransport, "publish_group", lambda *_, **__: {})
    monkeypatch.setattr(supervisor, "run_collection", collect)
    monkeypatch.setattr(worker.time, "monotonic", lambda: 1000)
    ownership = {"enabled": True, "identity_handoff_ready": True, "owner_id": "remote-test",
                 "manifest_id": "manifest", "provider_slots": [{"source": source, "account_id": "test",
                 "owner": "remote", "local_enabled": False, "market": "kr" if source == "krx" else "us", "domain": "prices"}]}
    worker.run_cycle(object(), tmp_path / "raw", ownership=ownership, planned_jobs=[], incoming_bundles=[],
                     output_root=tmp_path / "out", transport_root=tmp_path / "store", stream_id="remote-test", sequence=1,
                     checkpoint={"raw_identity_index": initial_index},
                     budget_seconds=600, source_handlers=handlers, source_handler_budget_seconds=600,
                     postprocess=lambda *_, **__: {}, postprocess_budget_seconds=60)
    assert calls["handler"] == expected_handler_budget
    assert calls.get("core") == expected_core_budget
    assert calls["durable_checkpoint"]["raw_identity_index"] == updated_index
