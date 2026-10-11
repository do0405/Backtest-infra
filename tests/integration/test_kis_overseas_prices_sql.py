from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import importlib
import json

import pytest
from sqlalchemy import event, func, select

from backtest_data.catalog.models import Security, SourceContract
from backtest_data.ingestion.http import ProviderResponse
from backtest_data.ingestion.jobs import LeaseLost
from backtest_data.ingestion.models import IngestionAttempt, RawObject
from backtest_data.ingestion.raw_store import RawStore
from backtest_data.prices.adapters.kis_overseas import KISOverseasDailyRequest
from backtest_data.prices.models import PriceObservation, VWAPDailyStat
from backtest_data.releases.models import SelectionDecision, ValidationIssue


pytestmark = pytest.mark.integration


def storage():
    name = "backtest_data.prices.kis_overseas_storage"
    assert importlib.util.find_spec(name) is not None, "KIS US SQL storage is required"
    return importlib.import_module(name).store_kis_overseas_prices


def security(session, *, market="US", symbol="AAPL"):
    value = Security(market=market, source_identifier="nasdaq_directory:" + symbol,
                     security_type="unclassified", name="Example")
    session.add(value)
    session.flush()
    return value


def request(**changes):
    return replace(KISOverseasDailyRequest("AAPL", date(2024, 1, 1), date(2024, 7, 9), "NAS",
        captured_at=datetime(2024, 7, 10, 18, tzinfo=timezone.utc)), **changes)


def mapping(identity, value=None, **changes):
    value = value or request()
    return {"market": "US", "security_id": identity.id, "provider_symbol": value.symbol,
            "exchange": value.exchange, "source": "kis_overseas_master",
            "qualified_current_mapping": True, "source_directory_hash": "a" * 64,
            "realtime_symbol": "D" + value.exchange + value.symbol} | changes


def bar(**changes):
    return {"xymd": "20240709", "open": "100.0100", "high": "110.1200", "low": "90.0000",
            "clos": "105.0100", "tvol": "100", "tamt": "10212.5000"} | changes


def payload(rows=None, **changes):
    # Exact AAPL spelling/shape was observed in the 2026-10-08 read-only probe.
    return {"rt_cd": "0", "output1": {"rsym": "DNASAAPL", "zdiv": "4", "nrec": "1"},
            "output2": [bar()] if rows is None else rows} | changes


def response(value, captured_at=None):
    return ProviderResponse(json.dumps(value).encode(), 200, captured_at or request().captured_at, "application/json")


def store(session, identity, value, raw_root, *, req=None, captured_at=None, proof=None):
    req = req or request()
    return storage()(session, identity.id, req, response(value, captured_at), value, raw_root,
                     mapping_evidence=mapping(identity, req) if proof is None else proof)


@pytest.mark.parametrize("incoming", [[], [bar(clos="NaN")], [bar(clos="106.0100")]])
def test_page_does_not_load_unrelated_price_history(db_session, tmp_path, incoming):
    identity = security(db_session)
    identity_id = identity.id
    store(db_session, identity, payload([bar(), bar(xymd="20240102")]), tmp_path)
    previous = db_session.scalar(select(PriceObservation.id).where(
        PriceObservation.security_id == identity_id, PriceObservation.session == date(2024, 7, 9)))
    db_session.expunge_all()
    identity = db_session.get(Security, identity_id)
    loaded = []

    def observe(session, instance):
        if isinstance(instance, PriceObservation):
            loaded.append(instance.session)

    event.listen(db_session, "loaded_as_persistent", observe)
    try:
        captured = request().captured_at + timedelta(days=1)
        result = store(db_session, identity, payload(incoming), tmp_path, captured_at=captured)
    finally:
        event.remove(db_session, "loaded_as_persistent", observe)
    assert set(loaded) == ({date(2024, 7, 9)} if result["inserted"] else set())
    assert result["inserted"] == (1 if incoming and incoming[0]["clos"] == "106.0100" else 0)
    if result["inserted"]:
        latest = db_session.scalar(select(PriceObservation).where(PriceObservation.security_id == identity_id,
            PriceObservation.captured_at == captured))
        assert latest.evidence["previous_observation_id"] == previous


def test_price_page_uses_bounded_writes_instead_of_per_bar_roundtrips(db_session, tmp_path):
    identity = security(db_session)
    rows = [bar(xymd=(date(2024, 7, 9)-timedelta(days=offset)).strftime("%Y%m%d")) for offset in range(51)]
    writes = {"price_observation": [], "selection_decision": []}

    def observe(connection, cursor, statement, parameters, context, many):
        if context.isinsert and context.compiled.statement.table.name in writes:
            writes[context.compiled.statement.table.name].append(len(parameters))

    event.listen(db_session.bind, "before_cursor_execute", observe)
    try:
        result = store(db_session, identity, payload(rows), tmp_path)
    finally:
        event.remove(db_session.bind, "before_cursor_execute", observe)
    assert result["inserted"] == 51
    assert len(writes["price_observation"]) == len(writes["selection_decision"]) == 6
    assert all(count <= 10 * len(PriceObservation.__table__.columns) for count in writes["price_observation"])
    assert all(count <= 10 * len(SelectionDecision.__table__.columns) for count in writes["selection_decision"])
    assert len(db_session.scalars(select(SelectionDecision).where(
        SelectionDecision.record_id.in_(select(PriceObservation.id).where(PriceObservation.security_id == identity.id)))).all()) == 51


def test_expired_fence_after_bulk_insert_rolls_back_entire_kis_page(db_session, tmp_path):
    identity = security(db_session)
    value = payload([bar(), bar(xymd="20240708")])

    def fence():
        if db_session.scalar(select(func.count()).select_from(PriceObservation).where(
            PriceObservation.security_id == identity.id)):
            raise LeaseLost("Expired or superseded lease")

    with pytest.raises(LeaseLost), db_session.begin_nested():
        storage()(db_session, identity.id, request(), response(value), value, tmp_path,
            mapping_evidence=mapping(identity), fence=fence)
    assert db_session.scalar(select(func.count()).select_from(PriceObservation).where(
        PriceObservation.security_id == identity.id)) == 0


def test_capture_replay_correction_reversion_and_unchanged_recaptures_are_preserved(db_session, tmp_path):
    identity = security(db_session)
    original = payload()
    first = response(original)
    result = store(db_session, identity, original, tmp_path)
    replay = store(db_session, identity, original, tmp_path)
    corrected = payload([bar(clos="106.0100")])
    store(db_session, identity, corrected, tmp_path, captured_at=first.captured_at + timedelta(days=1))
    store(db_session, identity, original, tmp_path, captured_at=first.captured_at + timedelta(days=2))
    store(db_session, identity, original, tmp_path, captured_at=first.captured_at + timedelta(days=3))
    rows = db_session.scalars(select(PriceObservation).where(PriceObservation.security_id == identity.id)
        .order_by(PriceObservation.captured_at)).all()
    assert [item.close for item in rows] == [Decimal("105.0100"), Decimal("106.0100"), Decimal("105.0100"), Decimal("105.0100")]
    assert rows[0].open.as_tuple() == Decimal("100.0100").as_tuple()
    assert rows[0].notional.as_tuple() == Decimal("10212.5000").as_tuple()
    assert replay["inserted"] == 0
    assert result["source_row_count"] == 1
    assert result["cursor_earliest_session"] == "2024-07-09"
    assert result["pagination_valid"] is True
    assert result["response_valid"] is True
    assert result["history_coverage_qualified"] is False
    assert all(item.currency == "USD" and item.feed == "kis:us:NAS" and item.venue == "NAS" for item in rows)
    assert all(item.available_at == item.captured_at for item in rows)
    assert all(item.evidence["historical_identity_verified"] is False and item.evidence["matched_vwap_eligible"] is False for item in rows)
    assert all(item.evidence["zdiv"] == "4" and item.evidence["rsym"] == "DNASAAPL" for item in rows)
    assert [item.evidence["previous_observation_id"] for item in rows] == [None, rows[0].id, rows[1].id, rows[2].id]
    assert db_session.scalar(select(func.count()).select_from(VWAPDailyStat)) == 0
    contract = db_session.get(SourceContract, rows[0].source_contract_id)
    assert contract.source == "kis_overseas" and contract.market == "US"
    assert contract.specification["raw_option"] == "0"
    assert contract.specification["action_history_complete"] is False
    assert contract.specification["matched_vwap"] is False
    raw = db_session.get(RawObject, rows[0].raw_id)
    assert RawStore(tmp_path).read(raw.storage_key) == first.content
    decisions = db_session.scalars(select(SelectionDecision).where(SelectionDecision.record_id.in_([item.id for item in rows]))).all()
    assert len(decisions) == 4
    assert all(item.rule_version == "kis_overseas_raw_price_structure_v1" for item in decisions)


def test_rejected_numeric_row_still_drives_backward_date_cursor(db_session, tmp_path):
    identity = security(db_session)
    value = payload([bar(), bar(xymd="20240708", clos="NaN")])
    result = store(db_session, identity, value, tmp_path)
    assert result["inserted"] == 1 and result["rejected"] == 1
    assert result["source_row_count"] == 2
    assert result["earliest_session"] == "2024-07-09"
    assert result["cursor_earliest_session"] == "2024-07-08"
    assert result["pagination_valid"] is True
    assert result["normalized_row_coverage_complete"] is False


@pytest.mark.parametrize("rows", [
    [bar(), bar()], [bar(xymd="20240708"), bar()], [bar(xymd="20240710")],
    [bar(xymd="20240230")], [bar(), {}], [None],
])
def test_malformed_or_nonprogressing_pages_fail_pagination_without_completing(db_session, tmp_path, rows):
    identity = security(db_session)
    result = store(db_session, identity, payload(rows), tmp_path)
    assert result["pagination_valid"] is False
    assert result["traversal_complete"] is False
    assert result["normalized_row_coverage_complete"] is False
    assert result["source_row_count"] == len(rows)
    assert result["rejected"] >= 1


def test_before_start_rows_mark_explicit_boundary_without_storing_out_of_range_prices(db_session, tmp_path):
    identity = security(db_session)
    value = payload([bar(xymd="20240102"), bar(xymd="20231229", clos="NaN")])
    result = store(db_session, identity, value, tmp_path)
    assert result["pagination_valid"] is True and result["boundary_reached"] is True
    assert result["source_row_count"] == 2 and result["processed_in_range"] == 1
    assert result["boundary_excluded_count"] == 1
    assert result["cursor_earliest_session"] == "2023-12-29"
    assert result["inserted"] == 1 and result["rejected"] == 0
    assert result["history_coverage_qualified"] is False
    assert db_session.scalar(select(func.count()).select_from(PriceObservation).where(PriceObservation.security_id == identity.id)) == 1


@pytest.mark.parametrize("changes", [
    {"rt_cd": "1"}, {"rt_cd": 0}, {"output2": {}}, {"output1": {}},
    {"output1": {"rsym": "DNASMSFT", "zdiv": "4"}},
    {"output1": {"rsym": "DNASAAPL", "zdiv": "-1"}},
])
def test_invalid_response_status_shape_or_verifiable_identity_is_archived(db_session, tmp_path, changes):
    identity = security(db_session)
    result = store(db_session, identity, payload(**changes), tmp_path)
    assert result["response_valid"] is False
    assert result["inserted"] == 0 and result["rejected"] >= 1
    assert result["normalized_row_coverage_complete"] is False
    assert db_session.scalar(select(func.count()).select_from(ValidationIssue)) >= 1
    assert db_session.scalar(select(func.count()).select_from(RawObject)) >= 1
    assert db_session.scalar(select(func.count()).select_from(IngestionAttempt)) >= 1


@pytest.mark.parametrize("changes", [
    {"market": "KR"}, {"security_id": "other"}, {"provider_symbol": "MSFT"}, {"exchange": "NYS"},
    {"qualified_current_mapping": False}, {"source_directory_hash": None}, {"realtime_symbol": None},
])
def test_verified_current_mapping_is_required_and_never_inferred_from_security_symbol(db_session, tmp_path, changes):
    identity = security(db_session)
    result = store(db_session, identity, payload(), tmp_path, proof=mapping(identity, **changes))
    assert result["response_valid"] is False and result["inserted"] == 0
    assert result["rejected"] >= 1


def test_missing_mapping_and_korean_security_fail_closed(db_session, tmp_path):
    identity = security(db_session, market="KR")
    value = payload()
    result = storage()(db_session, identity.id, request(), response(value), value, tmp_path)
    assert result["inserted"] == 0 and result["response_valid"] is False
    assert db_session.scalar(select(func.count()).select_from(PriceObservation).where(PriceObservation.security_id == identity.id)) == 0


def test_valid_proof_cannot_cross_korean_security_market(db_session, tmp_path):
    identity = security(db_session, market="KR")
    result = store(db_session, identity, payload(), tmp_path)
    assert result["response_valid"] is False and result["inserted"] == 0


@pytest.mark.parametrize("changes", [{"rsym": "DNASMSFT"}, {"zdiv": "2"}])
def test_contradictory_row_identity_or_precision_is_rejected(db_session, tmp_path, changes):
    identity = security(db_session)
    result = store(db_session, identity, payload([bar(**changes)]), tmp_path)
    assert result["rejected"] == 1 and result["inserted"] == 0
    assert result["cursor_earliest_session"] == "2024-07-09"


def test_out_of_order_capture_links_only_to_known_prior_capture(db_session, tmp_path):
    identity = security(db_session)
    initial = request().captured_at
    store(db_session, identity, payload(), tmp_path)
    store(db_session, identity, payload(), tmp_path, captured_at=initial + timedelta(days=2))
    store(db_session, identity, payload([bar(clos="106.0100")]), tmp_path, captured_at=initial + timedelta(days=1))
    rows = db_session.scalars(select(PriceObservation).where(PriceObservation.security_id == identity.id)
        .order_by(PriceObservation.captured_at)).all()
    assert rows[1].evidence["previous_observation_id"] == rows[0].id


def test_same_capture_in_different_timezone_is_idempotent(db_session, tmp_path):
    identity = security(db_session)
    store(db_session, identity, payload(), tmp_path)
    shifted = request().captured_at.astimezone(timezone(timedelta(hours=9)))
    replay = store(db_session, identity, payload(), tmp_path, captured_at=shifted)
    assert replay["inserted"] == 0
    assert db_session.scalar(select(func.count()).select_from(PriceObservation).where(PriceObservation.security_id == identity.id)) == 1


def test_proven_class_symbol_is_preserved_without_rewriting_directory_identifier(db_session, tmp_path):
    identity = security(db_session, symbol="BRK.B")
    req = request(symbol="BRK/B", exchange="NYS")
    value = payload(output1={"rsym": "DNYSBRK/B", "zdiv": "4"})
    result = store(db_session, identity, value, tmp_path, req=req)
    assert result["inserted"] == 1
    row = db_session.scalar(select(PriceObservation).where(PriceObservation.security_id == identity.id))
    assert row.evidence["source_symbol"] == "BRK/B" and row.feed == "kis:us:NYS"
    assert identity.source_identifier == "nasdaq_directory:BRK.B"


def test_empty_valid_history_keeps_capture_evidence_and_is_unavailable(db_session, tmp_path):
    identity = security(db_session)
    result = store(db_session, identity, payload([]), tmp_path)
    assert result["response_valid"] is True and result["pagination_valid"] is True
    assert result["source_row_count"] == 0
    assert result["empty_history_unavailable"] is True
    assert result["normalized_row_coverage_complete"] is False
    assert result["cursor_earliest_session"] is None


def test_canonical_decimal_json_remains_exact_in_sql(db_session, tmp_path):
    identity = security(db_session)
    content = b'{"rt_cd":"0","output1":{"rsym":"DNASAAPL","zdiv":"4"},"output2":[{"xymd":"20240709","open":100.0100,"high":110.1200,"low":90.0000,"clos":105.0100,"tvol":100,"tamt":10212.5000}]}'
    value = json.loads(content, parse_float=Decimal)
    result = storage()(db_session, identity.id, request(), ProviderResponse(content, 200, request().captured_at, "application/json"),
        value, tmp_path, mapping_evidence=mapping(identity))
    assert result["inserted"] == 1
    row = db_session.scalar(select(PriceObservation).where(PriceObservation.security_id == identity.id))
    assert row.open.as_tuple() == Decimal("100.0100").as_tuple()
    assert row.notional.as_tuple() == Decimal("10212.5000").as_tuple()


def test_naive_capture_is_rejected_before_sql_observation(db_session, tmp_path):
    identity = security(db_session)
    with pytest.raises(ValueError, match="aware"):
        store(db_session, identity, payload(), tmp_path, captured_at=datetime(2024, 7, 10, 18))


def test_rejected_evidence_flush_is_fenced_before_autoflush_and_rolls_back(db_session, tmp_path):
    identity = security(db_session)
    rows = [bar(xymd=(date(2024, 7, 9) - timedelta(days=index)).strftime("%Y%m%d"), clos="NaN")
        for index in range(13)]
    value = payload(rows)
    prior_attempts = db_session.scalar(select(func.count()).select_from(IngestionAttempt))
    prior_raw = db_session.scalar(select(func.count()).select_from(RawObject))
    seen = []

    def fence():
        count = db_session.scalar(select(func.count()).select_from(ValidationIssue).where(
            ValidationIssue.evidence["security_id"].astext == identity.id))
        seen.append(count)
        if count:
            raise LeaseLost("Expired or superseded lease")

    with pytest.raises(LeaseLost), db_session.begin_nested():
        storage()(db_session, identity.id, request(), response(value), value, tmp_path,
            mapping_evidence=mapping(identity), fence=fence)

    assert seen[-2:] == [0, 10]
    assert max(seen) == 10
    assert db_session.scalar(select(func.count()).select_from(ValidationIssue).where(
        ValidationIssue.evidence["security_id"].astext == identity.id)) == 0
    assert db_session.scalar(select(func.count()).select_from(IngestionAttempt)) == prior_attempts
    assert db_session.scalar(select(func.count()).select_from(RawObject)) == prior_raw
