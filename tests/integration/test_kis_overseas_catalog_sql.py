from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib
import io
import zipfile
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select

from backtest_data.catalog.models import CatalogObservation, Security
from backtest_data.db import new_id
from backtest_data.ingestion.http import ProviderError, ProviderResponse
from backtest_data.ingestion.models import CollectionJob, RawObject, TargetUniverseManifest
from backtest_data.ingestion.raw_store import RawStore


pytestmark = pytest.mark.integration
STAMP = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


def catalog():
    return importlib.import_module("backtest_data.catalog.kis_overseas")


def zipped(exchange, symbols):
    rows = []
    for symbol in symbols:
        rows.append("\t".join(["US", {"NAS": "22", "NYS": "21", "AMS": "23"}[exchange], exchange,
            "거래소", symbol, exchange + symbol, "이름", "EXAMPLE", "2", "USD", "4", "", "1.0000",
            "1", "1", "930", "1600", "N", "", "000", "0", "0", "   ", "   "]))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr(zipfile.ZipInfo(exchange + "MST.COD"), "\r\n".join(rows).encode("cp949"),
            compress_type=zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


def target(session, us_symbols=("AAPL", "BRK.B", "BRK/B", "DUP"), *, include_kr=True):
    members = []
    for market, symbol in [("US", symbol) for symbol in us_symbols] + ([("KR", "AAPL")] if include_kr else []):
        identity = Security(market=market, source_identifier=market + ":directory:" + symbol,
            name=symbol, security_type="unclassified")
        session.add(identity)
        session.flush()
        members.append({"market": market, "security_id": identity.id, "symbol": symbol,
            "identifier": identity.source_identifier, "venue": "ARCA", "security_type": "unclassified"})
    value = TargetUniverseManifest(content_hash=new_id().replace("-", "") * 2,
        scope={"output_start": "2016-01-01", "output_end": "2026-10-07"}, members=members, gaps=[])
    session.add(value)
    session.flush()
    return value


def masters(session, raw_root, *, stamp=STAMP, nas=("AAPL", "DUP")):
    captures = []
    for exchange, url in catalog().MASTER_DIRECTORIES:
        symbols = nas if exchange == "NAS" else ("BRK/B", "DUP") if exchange == "NYS" else ("OTHER",)
        content = zipped(exchange, symbols)
        raw = RawStore(raw_root).put(content)
        existing = session.scalar(select(RawObject).where(RawObject.content_hash == raw.content_hash))
        if existing is None:
            existing = RawObject(content_hash=raw.content_hash, storage_key=raw.storage_key, byte_count=raw.byte_count)
            session.add(existing)
            session.flush()
        captures.append({"exchange": exchange, "url": url, "raw_id": existing.id, "hash": raw.content_hash,
            "captured_at": stamp.isoformat(), "rows": catalog().parse_kis_overseas_master(content, exchange)})
    return captures


def jobs(session):
    return session.scalars(select(CollectionJob).where(CollectionJob.source == "kis_overseas")).all()


def test_bulk_planner_keeps_unsupported_us_denominator_and_archived_mapping_refs(db_session, tmp_path):
    manifest = target(db_session)
    result = catalog().save_kis_overseas_catalog(db_session, manifest, masters(db_session, tmp_path))
    values = jobs(db_session)
    assert result["target_count"] == 4 and result["mapped_count"] == 2
    assert result["unmapped_count"] == 1 and result["ambiguous_count"] == 1
    assert result["planned_jobs"] == 4
    assert len(values) == 4 and {value.request["market"] for value in values} == {"US"}
    assert {value.entity_id for value in values} == {item["security_id"] for item in manifest.members if item["market"] == "US"}
    by_symbol = {value.request["symbol"]: value for value in values}
    assert by_symbol["BRK.B"].status == "unsupported"
    assert by_symbol["BRK.B"].checkpoint["reason"] == "kis_overseas_mapping_unavailable"
    assert by_symbol["DUP"].checkpoint["reason"] == "kis_overseas_mapping_ambiguous"
    assert by_symbol["AAPL"].request["provider_symbol"] == "AAPL"
    assert by_symbol["AAPL"].request["exchange"] == "NAS"
    proof = by_symbol["AAPL"].request["mapping_evidence"]
    observation = db_session.get(CatalogObservation, proof["catalog_observation_id"])
    assert observation.identifier == "US:directory:AAPL"
    assert observation.fields["master_raw_id"] == proof["master_raw_id"]
    assert observation.fields["master_hash"] == proof["master_hash"]
    assert db_session.get(RawObject, proof["master_raw_id"]).content_hash == proof["master_hash"]
    assert proof["historical_identity_verified"] is False
    assert len(manifest.members) == 5 and manifest.gaps == []


def test_replay_and_new_capture_keep_evidence_without_replanning_same_work(db_session, tmp_path):
    manifest = target(db_session)
    first = catalog().save_kis_overseas_catalog(db_session, manifest, masters(db_session, tmp_path))
    original = {value.id: value.request for value in jobs(db_session)}
    replay = catalog().save_kis_overseas_catalog(db_session, manifest, masters(db_session, tmp_path))
    assert replay["planned_jobs"] == 0 and replay["reused_jobs"] == 4
    assert db_session.scalar(select(func.count()).select_from(CatalogObservation)) == 4
    catalog().save_kis_overseas_catalog(db_session, manifest,
        masters(db_session, tmp_path, stamp=STAMP + timedelta(days=1)))
    assert db_session.scalar(select(func.count()).select_from(CatalogObservation)) == 8
    assert {value.id: value.request for value in jobs(db_session)} == original
    assert db_session.scalar(select(func.count()).select_from(RawObject)) == 3
    assert first["historical_identity_verified"] is False


def test_changed_master_capture_with_unchanged_mapping_reuses_jobs(db_session, tmp_path):
    manifest = target(db_session)
    catalog().save_kis_overseas_catalog(db_session, manifest, masters(db_session, tmp_path))
    result = catalog().save_kis_overseas_catalog(db_session, manifest,
        masters(db_session, tmp_path, stamp=STAMP + timedelta(days=1), nas=("AAPL", "DUP", "NEW")))
    assert result["planned_jobs"] == 0 and len(jobs(db_session)) == 4


def test_changed_exact_provider_exchange_gets_distinct_work(db_session, tmp_path):
    manifest = target(db_session, ("AAPL",))
    first = masters(db_session, tmp_path)
    catalog().save_kis_overseas_catalog(db_session, manifest, first)
    second = []
    for capture in first:
        exchange = capture["exchange"]
        content = zipped(exchange, ("AAPL",) if exchange == "NYS" else ("OTHER" + exchange,))
        raw = RawStore(tmp_path).put(content)
        value = RawObject(content_hash=raw.content_hash, storage_key=raw.storage_key, byte_count=raw.byte_count)
        db_session.add(value)
        db_session.flush()
        second.append({**capture, "raw_id": value.id, "hash": raw.content_hash,
            "rows": catalog().parse_kis_overseas_master(content, exchange)})
    result = catalog().save_kis_overseas_catalog(db_session, manifest, second)
    assert result["planned_jobs"] == 1
    assert {value.request["exchange"] for value in jobs(db_session)} == {"NAS", "NYS"}


def test_raw_hash_mismatch_is_rejected_before_mapping_or_job_writes(db_session, tmp_path):
    manifest = target(db_session)
    captures = masters(db_session, tmp_path)
    captures[0] = {**captures[0], "hash": "0" * 64}
    with pytest.raises(ValueError, match="raw reference hash"):
        catalog().save_kis_overseas_catalog(db_session, manifest, captures)
    assert jobs(db_session) == []
    assert db_session.scalar(select(func.count()).select_from(CatalogObservation)) == 0


@pytest.mark.parametrize("changes", [{"market": "KR"}, {"security_id": "another"},
    {"provider_symbol": "APPLE"}, {"qualified_current_mapping": False}, {"master_raw_id": None}])
def test_standalone_planner_rejects_unverified_mapping_evidence(db_session, tmp_path, changes):
    manifest = target(db_session, ("AAPL",))
    mappings = catalog().resolve_kis_overseas_mappings(manifest.members, masters(db_session, tmp_path))
    identity = manifest.members[0]["security_id"]
    mappings[identity]["mapping_evidence"].update(changes)
    with pytest.raises(ValueError, match="mapping evidence"):
        catalog().plan_kis_overseas_jobs(db_session, manifest, mappings)
    assert jobs(db_session) == []


def test_capture_timestamp_is_normalized_and_naive_timestamp_rejected(db_session, tmp_path):
    manifest = target(db_session, ("AAPL",))
    captures = masters(db_session, tmp_path)
    captures[0] = {**captures[0], "captured_at": STAMP.replace(tzinfo=None)}
    with pytest.raises(ValueError, match="timezone-aware"):
        catalog().save_kis_overseas_catalog(db_session, manifest, captures)
    assert jobs(db_session) == []
    captures[0] = {**captures[0], "captured_at": STAMP}
    catalog().save_kis_overseas_catalog(db_session, manifest, captures)
    assert jobs(db_session)[0].request["mapping_evidence"]["master_captured_at"] == STAMP.isoformat()


def test_later_complete_master_creates_qualified_work_and_preserves_unsupported_history(db_session, tmp_path):
    manifest = target(db_session)
    captures = masters(db_session, tmp_path)
    result = catalog().save_kis_overseas_catalog(db_session, manifest, captures[:1])
    assert result["target_count"] == 4 and result["mapped_count"] == 0
    assert all(value.status == "unsupported" for value in jobs(db_session))
    catalog().save_kis_overseas_catalog(db_session, manifest, captures)
    values = jobs(db_session)
    assert len(values) == 8
    assert len({value.entity_id for value in values}) == 4
    assert sum(value.status == "pending" for value in values) == 2
    assert sum(value.status == "unsupported" for value in values) == 6


def test_manifest_market_and_identifier_must_match_security(db_session, tmp_path):
    manifest = target(db_session, ("AAPL",))
    forged = SimpleNamespace(id=manifest.id, scope=manifest.scope, members=[dict(manifest.members[1], market="US")])
    with pytest.raises(ValueError, match="security"):
        catalog().save_kis_overseas_catalog(db_session, forged, masters(db_session, tmp_path))
    assert jobs(db_session) == []


def test_empty_us_target_cannot_silently_plan_a_zero_denominator(db_session):
    manifest = SimpleNamespace(id="empty", scope={"output_start": "2016-01-01", "output_end": "2026-10-07"},
        members=[{"market": "KR"}])
    with pytest.raises(ValueError, match="no US"):
        catalog().plan_kis_overseas_jobs(db_session, manifest, {})


def test_planning_queries_are_batched_instead_of_per_target(db_session, tmp_path):
    manifest = target(db_session, tuple("TARGET" + str(index) for index in range(1250)))
    captures = masters(db_session, tmp_path)
    queries = []
    def count_query(connection, cursor, statement, params, context, many):
        queries.append(statement)
    event.listen(db_session.bind, "before_cursor_execute", count_query)
    try:
        result = catalog().save_kis_overseas_catalog(db_session, manifest, captures)
    finally:
        event.remove(db_session.bind, "before_cursor_execute", count_query)
    assert result["planned_jobs"] == 1250
    assert len(queries) < 25


def test_sync_keeps_invalid_download_raw_and_provider_gaps(db_session, tmp_path, monkeypatch):
    manifest = target(db_session, ("AAPL",))
    @contextmanager
    def in_test_transaction(engine):
        yield db_session
    monkeypatch.setattr(catalog(), "transaction", in_test_transaction)
    class HTTP:
        def request(self, source, method, url):
            assert source == "directory" and method == "GET" and url.startswith("https://")
            if "nysmst" in url:
                raise ProviderError("directory", "transport_failed", retryable=True)
            content = b"invalid zip" if "amsmst" in url else zipped("NAS", ("AAPL",))
            return ProviderResponse(content, 200, STAMP, "application/zip")
    result = catalog().sync_kis_overseas_catalog(object(), HTTP(), tmp_path, manifest_id=manifest.id)
    assert result["target_count"] == 1 and result["mapped_count"] == 0
    assert len(result["evidence"]) == 2
    assert db_session.scalar(select(func.count()).select_from(RawObject)) == 2
    assert {gap["reason"] for gap in result["gaps"]} >= {"transport_failed", "kis_overseas_master_invalid", "kis_overseas_master_incomplete"}
    invalid = next(value for value in result["evidence"] if value["exchange"] == "AMS")
    assert invalid["parse_status"] == "invalid"
    assert RawStore(tmp_path).read(db_session.get(RawObject, invalid["raw_id"]).storage_key) == b"invalid zip"


def test_sync_uses_latest_manifest_and_real_session_lifecycle(db_session, tmp_path):
    from backtest_data.db import transaction
    connection = db_session.connection()
    # Commit only a savepoint; the fixture's external test transaction rolls
    # back all rows. This exercises actual expiring Session boundaries.
    with transaction(connection) as session:
        older = target(session, ("OLD",), include_kr=False)
        newer = target(session, ("AAPL",))
        newer_id = newer.id
    class HTTP:
        def request(self, source, method, url):
            exchange = next(exchange for exchange, master_url in catalog().MASTER_DIRECTORIES if url == master_url)
            return ProviderResponse(zipped(exchange, ("AAPL",) if exchange == "NAS" else ("OTHER" + exchange,)),
                200, STAMP, "application/zip")
    result = catalog().sync_kis_overseas_catalog(connection, HTTP(), tmp_path)
    assert result["manifest_id"] == newer_id
    assert result["target_count"] == 1 and result["mapped_count"] == 1
    assert result["planned_jobs"] == 1 and len(result["evidence"]) == 3
