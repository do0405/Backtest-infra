from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import importlib
import json
from time import perf_counter

import pytest
from sqlalchemy import event, func, select

from backtest_data.catalog.models import Security, SourceContract
from backtest_data.ingestion.http import ProviderResponse
from backtest_data.ingestion.jobs import LeaseLost
from backtest_data.ingestion.models import IngestionAttempt, RawObject
from backtest_data.ingestion.raw_store import RawStore
from backtest_data.prices.adapters.alpaca import AlpacaDailyRequest, NEW_YORK
from backtest_data.prices.models import PriceObservation, VWAPDailyStat
from backtest_data.releases.models import SelectionDecision, ValidationIssue


pytestmark = pytest.mark.integration


def storage():
    assert importlib.util.find_spec("backtest_data.prices.alpaca_storage") is not None, "Alpaca SQL storage is required"
    return importlib.import_module("backtest_data.prices.alpaca_storage").store_alpaca_prices


def security(session):
    value = Security(market="US", source_identifier="nasdaq_directory:AAPL", security_type="unclassified", name="Example")
    session.add(value)
    session.flush()
    return value


def request(**changes):
    return replace(AlpacaDailyRequest(date(2016, 1, 1), date(2024, 7, 10), symbol="AAPL",
        captured_at=datetime(2024, 7, 10, 18, tzinfo=timezone.utc)), **changes)


def bar(**changes):
    return {"t": "2024-07-09T04:00:00Z", "o": "100.0100", "h": "110", "l": "90", "c": "105", "v": 100, "vw": "102.125", "n": 10} | changes


def response(payload, captured_at=None):
    content = json.dumps(payload).encode()
    return ProviderResponse(content, 200, captured_at or request().captured_at, "application/json")


@pytest.mark.parametrize("incoming", [[], [bar(c="NaN")], [bar(c="106")]])
def test_page_does_not_load_unrelated_price_history(db_session, tmp_path, incoming):
    identity = security(db_session)
    identity_id = identity.id
    historical = {"symbol": "AAPL", "bars": [bar(), bar(t="2016-01-04T05:00:00Z")], "next_page_token": None}
    storage()(db_session, identity_id, request(), response(historical), historical, tmp_path)
    previous = db_session.scalar(select(PriceObservation.id).where(
        PriceObservation.security_id == identity_id, PriceObservation.session == date(2024, 7, 9)))
    db_session.expunge_all()
    loaded = []

    def observe(session, instance):
        if isinstance(instance, PriceObservation):
            loaded.append(instance.session)

    event.listen(db_session, "loaded_as_persistent", observe)
    try:
        page = {"symbol": "AAPL", "bars": incoming, "next_page_token": None}
        captured = request().captured_at + timedelta(days=1)
        result = storage()(db_session, identity_id, request(), response(page, captured), page, tmp_path)
    finally:
        event.remove(db_session, "loaded_as_persistent", observe)
    assert set(loaded) == ({date(2024, 7, 9)} if result["inserted"] else set())
    assert result["inserted"] == (1 if incoming and incoming[0]["c"] == "106" else 0)
    if result["inserted"]:
        latest = db_session.scalar(select(PriceObservation).where(PriceObservation.security_id == identity_id,
            PriceObservation.captured_at == captured))
        assert latest.evidence["previous_observation_id"] == previous


@pytest.fixture
def capture_storage_queries(db_session):
    @contextmanager
    def capture():
        queries = []

        def count_query(connection, cursor, statement, parameters, context, many):
            table = context.compiled.statement.table.name if context.isinsert else None
            queries.append({"table": table, "bind_count": len(parameters)})

        event.listen(db_session.bind, "before_cursor_execute", count_query)
        try:
            yield queries
        finally:
            event.remove(db_session.bind, "before_cursor_execute", count_query)

    return capture


def test_capture_events_are_immutable_and_identical_replay_idempotent(db_session, tmp_path):
    store = storage()
    identity = security(db_session)
    payload = {"symbol": "AAPL", "bars": [bar()], "next_page_token": None}
    first = response(payload)
    result = store(db_session, identity.id, request(), first, payload, tmp_path)
    store(db_session, identity.id, request(), first, payload, tmp_path)
    newer = response(payload, first.captured_at + timedelta(days=2))
    store(db_session, identity.id, request(), newer, payload, tmp_path)
    changed = payload | {"bars": [bar(c="106")]}
    store(db_session, identity.id, request(), response(changed, first.captured_at + timedelta(days=1)), changed, tmp_path)
    rows = db_session.scalars(select(PriceObservation).where(PriceObservation.security_id == identity.id).order_by(PriceObservation.captured_at)).all()
    assert [row.close for row in rows] == [Decimal("105"), Decimal("106"), Decimal("105")]
    assert rows[0].open == Decimal("100.0100")
    assert rows[0].session == date(2024, 7, 9)
    assert all(row.notional is None and row.feed == "sip" and row.currency == "USD" and row.basis == "as_traded" for row in rows)
    assert all(row.evidence["matched_vwap_eligible"] is False for row in rows)
    assert rows[0].evidence["provider_vwap"] == "102.125"
    assert rows[0].evidence["provider_vwap_status"] == "supplier_reported_unverified"
    assert rows[0].evidence["provider_vwap_volume_matches_bar_volume"] is None
    assert rows[0].evidence["historical_identity_verified"] is False
    assert rows[0].evidence["previous_observation_id"] is None
    assert rows[1].evidence["previous_observation_id"] == rows[2].id
    assert rows[2].evidence["previous_observation_id"] == rows[0].id
    assert rows[0].evidence["source_row_hash"] == rows[2].evidence["source_row_hash"]
    assert rows[0].evidence["source_row_hash"] != rows[1].evidence["source_row_hash"]
    assert rows[0].signature != rows[2].signature
    assert all(row.available_at == row.captured_at for row in rows)
    assert db_session.scalar(select(func.count()).select_from(VWAPDailyStat)) == 0
    decisions = db_session.scalars(select(SelectionDecision).where(SelectionDecision.record_id.in_([row.id for row in rows]))).all()
    assert len(decisions) == 3
    contract = db_session.get(SourceContract, rows[0].source_contract_id)
    assert contract.specification["feed"] == "sip"
    assert contract.specification["calendar_qualified"] is False
    raw = db_session.get(RawObject, rows[0].raw_id)
    assert RawStore(tmp_path).read(raw.storage_key) == first.content
    assert result["target_end"] == "2024-07-10"
    assert result["effective_end"] == "2024-07-09"
    assert result["request_anchor"] == request().captured_at.isoformat()
    assert result["page_size"] == 10000
    assert result["deferred_recent_start"] == "2024-07-10"
    assert result["traversal_complete"] is True


@pytest.mark.parametrize("rows", [[], [bar(c=100.1)], [bar(t="2024-07-10T04:00:00Z")], [bar(t="2024-07-09T00:00:00Z")], [bar(o="0", l="0")]])
def test_empty_or_all_rejected_page_keeps_api_token_for_traversal(db_session, tmp_path, rows):
    store = storage()
    identity = security(db_session)
    payload = {"symbol": "AAPL", "bars": rows, "next_page_token": "opaque_next=="}
    result = store(db_session, identity.id, request(), response(payload), payload, tmp_path)
    assert result["next_page_token"] == "opaque_next=="
    assert result["traversal_complete"] is False
    assert result["inserted"] == 0
    assert result["rejected"] == len(rows)
    assert result["source_row_count"] == len(rows)
    if rows:
        assert result["normalized_row_coverage_complete"] is False
        assert db_session.scalar(select(func.count()).select_from(ValidationIssue)) >= 1


def test_empty_exhausted_history_is_unavailable_and_retains_sql_raw_evidence(db_session, tmp_path):
    store = storage()
    identity = security(db_session)
    payload = {"symbol": "AAPL", "bars": None, "next_page_token": None}
    result = store(db_session, identity.id, request(), response(payload), payload, tmp_path)
    assert result["traversal_complete"] is True
    assert result["empty_history_unavailable"] is True
    assert result["normalized_row_coverage_complete"] is False
    assert db_session.scalar(select(func.count()).select_from(RawObject)) >= 1
    assert db_session.scalar(select(func.count()).select_from(IngestionAttempt)) >= 1


@pytest.mark.parametrize("changes", [{"symbol": "MSFT"}, {"bars": {"AAPL": [bar()]}}, {"next_page_token": 123}])
def test_response_shape_or_identity_failure_is_archived_and_not_accepted(db_session, tmp_path, changes):
    store = storage()
    identity = security(db_session)
    payload = {"symbol": "AAPL", "bars": [bar()], "next_page_token": None} | changes
    result = store(db_session, identity.id, request(), response(payload), payload, tmp_path)
    assert result["inserted"] == 0
    assert result["rejected"] >= 1
    assert result["normalized_row_coverage_complete"] is False
    assert db_session.scalar(select(func.count()).select_from(PriceObservation).where(PriceObservation.security_id == identity.id)) == 0
    assert db_session.scalar(select(func.count()).select_from(ValidationIssue)) >= 1
    assert db_session.scalar(select(func.count()).select_from(RawObject)) >= 1


def test_canonical_decimal_json_is_stored_exactly(db_session, tmp_path):
    store = storage()
    identity = security(db_session)
    content = b'{"symbol":"AAPL","bars":[{"t":"2024-07-09T04:00:00Z","o":100.0100,"h":110,"l":90,"c":105,"v":100,"vw":102.125}],"next_page_token":null}'
    payload = json.loads(content, parse_float=Decimal)
    captured = request().captured_at
    result = store(db_session, identity.id, request(), ProviderResponse(content, 200, captured, "application/json"), payload, tmp_path)
    assert result["inserted"] == 1
    assert db_session.scalar(select(PriceObservation.open).where(PriceObservation.security_id == identity.id)) == Decimal("100.0100")


def test_large_page_uses_bounded_price_and_decision_batches(db_session, tmp_path, capture_storage_queries, record_property):
    store = storage()
    identity = security(db_session)
    bars = [bar(t=datetime.combine(date(2016, 1, 1) + timedelta(days=index), datetime.min.time(), NEW_YORK).isoformat())
            for index in range(1251)]
    # Include duplicate captures across the batch boundary and malformed rows.
    rows = bars[:500] + [bars[0]] + bars[500:] + [bars[0], bar(c=100.1), None]
    payload = {"symbol": "AAPL", "bars": rows, "next_page_token": "opaque_next=="}
    captured = response(payload)
    started_at = perf_counter()
    with capture_storage_queries() as queries:
        result = store(db_session, identity.id, request(), captured, payload, tmp_path)
    elapsed = perf_counter() - started_at

    price_writes = [query for query in queries if query["table"] == "price_observation"]
    decision_writes = [query for query in queries if query["table"] == "selection_decision"]
    record_property("page_storage_seconds", elapsed)
    record_property("page_sql_statements", len(queries))
    record_property("page_price_inserts", len(price_writes))
    record_property("page_decision_inserts", len(decision_writes))
    assert 1 <= len(price_writes) <= 126, f"Expected at most 126 price inserts, got {len(price_writes)}"
    assert 1 <= len(decision_writes) <= 126, f"Expected at most 126 decision inserts, got {len(decision_writes)}"
    assert all(query["bind_count"] <= 10 * len(PriceObservation.__table__.columns) for query in price_writes)
    assert all(query["bind_count"] <= 10 * len(SelectionDecision.__table__.columns) for query in decision_writes)
    assert len(queries) < 270, f"Expected a bounded page write, got {len(queries)} statements"
    assert result["inserted"] == 1251
    assert result["rejected"] == 2
    assert result["source_row_count"] == 1255
    assert result["earliest_session"] == "2016-01-01"
    assert result["latest_session"] == (date(2016, 1, 1) + timedelta(days=1250)).isoformat()
    assert result["next_page_token"] == "opaque_next=="
    assert result["traversal_complete"] is False
    assert result["normalized_row_coverage_complete"] is False
    observations = db_session.scalars(select(PriceObservation).where(PriceObservation.security_id == identity.id)).all()
    assert len(observations) == 1251
    assert all(row.evidence["previous_observation_id"] is None for row in observations)
    assert all(row.available_at == row.captured_at == captured.captured_at for row in observations)
    assert all(row.notional is None and row.evidence["matched_vwap_eligible"] is False for row in observations)
    decisions = db_session.scalars(select(SelectionDecision).where(SelectionDecision.record_id.in_([row.id for row in observations]))).all()
    assert len(decisions) == 1251
    assert {decision.record_id for decision in decisions} == {row.id for row in observations}
    assert all(decision.status == "accepted" and decision.rule_version == "alpaca_raw_price_structure_v1" for decision in decisions)
    issues = db_session.scalars(select(ValidationIssue).where(ValidationIssue.evidence["security_id"].astext == identity.id)).all()
    assert {issue.evidence["row_index"] for issue in issues} == {1253, 1254}
    assert all(issue.rule == "source_row_validation" for issue in issues)


def test_same_capture_duplicates_and_revisions_only_decide_new_rows(db_session, tmp_path):
    store = storage()
    identity = security(db_session)
    initial_payload = {"symbol": "AAPL", "bars": [bar()], "next_page_token": None}
    store(db_session, identity.id, request(), response(initial_payload), initial_payload, tmp_path)
    previous = db_session.scalar(select(PriceObservation).where(PriceObservation.security_id == identity.id))
    payload = {"symbol": "AAPL", "bars": [bar(c="106"), bar(c="106"), bar(c="107")], "next_page_token": None}
    captured_at = response(initial_payload).captured_at + timedelta(days=1)
    first = store(db_session, identity.id, request(), response(payload, captured_at), payload, tmp_path)
    replay = store(db_session, identity.id, request(), response(payload, captured_at), payload, tmp_path)

    assert first["inserted"] == 2
    assert first["source_row_count"] == 3
    assert first["normalized_row_coverage_complete"] is True
    assert replay["inserted"] == 0
    observations = db_session.scalars(select(PriceObservation).where(PriceObservation.security_id == identity.id)).all()
    assert len(observations) == 3
    revisions = [row for row in observations if row.captured_at == captured_at]
    assert {row.close for row in revisions} == {Decimal("106"), Decimal("107")}
    assert all(row.evidence["previous_observation_id"] == previous.id for row in revisions)
    decisions = db_session.scalars(select(SelectionDecision).where(SelectionDecision.record_id.in_([row.id for row in observations]))).all()
    assert len(decisions) == 3


def test_optional_fence_runs_before_previous_query_and_after_each_write_chunk(db_session, tmp_path):
    identity = security(db_session)
    bars = [bar(t=datetime.combine(date(2016, 1, 1) + timedelta(days=index), datetime.min.time(), NEW_YORK).isoformat())
            for index in range(61)]
    payload = {"symbol": "AAPL", "bars": bars, "next_page_token": None}
    fences = []

    def fence():
        observations = db_session.scalar(select(func.count()).select_from(PriceObservation).where(PriceObservation.security_id == identity.id))
        decisions = db_session.scalar(select(func.count()).select_from(SelectionDecision).where(
            SelectionDecision.record_id.in_(select(PriceObservation.id).where(PriceObservation.security_id == identity.id))))
        fences.append((observations, decisions))

    result = storage()(db_session, identity.id, request(), response(payload), payload, tmp_path, fence=fence)

    assert result["inserted"] == 61
    expected = [(0, 0)] * 7  # Six metadata checks and the prior-read check.
    for inserted in [*range(10, 61, 10), 61]:
        expected.extend([expected[-1], (inserted, expected[-1][1]), (inserted, inserted)])
    expected.extend([(61, 61), (61, 61)])
    assert fences == expected


@pytest.mark.parametrize("fail_at", [7, 8, 9, 10, 11, 12])
def test_lease_loss_from_optional_fence_propagates_and_page_transaction_rolls_back(db_session, tmp_path, fail_at):
    identity = security(db_session)
    payload = {"symbol": "AAPL", "bars": [bar(), bar(c="106")], "next_page_token": None}
    calls = []
    prior_attempts = db_session.scalar(select(func.count()).select_from(IngestionAttempt))
    prior_decisions = db_session.scalar(select(func.count()).select_from(SelectionDecision))

    def fence():
        prices = db_session.scalar(select(func.count()).select_from(PriceObservation).where(PriceObservation.security_id == identity.id))
        decisions = db_session.scalar(select(func.count()).select_from(SelectionDecision)) - prior_decisions
        calls.append((prices, decisions))
        if len(calls) == fail_at:
            raise LeaseLost("Expired or superseded lease")

    with pytest.raises(LeaseLost), db_session.begin_nested():
        storage()(db_session, identity.id, request(), response(payload), payload, tmp_path, fence=fence)

    assert len(calls) == fail_at
    assert calls[-1] == {7: (0, 0), 8: (0, 0), 9: (2, 0), 10: (2, 2), 11: (2, 2), 12: (2, 2)}[fail_at]
    assert db_session.scalar(select(func.count()).select_from(PriceObservation).where(PriceObservation.security_id == identity.id)) == 0
    assert db_session.scalar(select(func.count()).select_from(IngestionAttempt)) == prior_attempts
    assert db_session.scalar(select(func.count()).select_from(SelectionDecision)) == prior_decisions


@pytest.mark.parametrize("page_size", [100, 10000])
def test_storage_checkpoint_preserves_api_page_size_and_original_window(db_session, tmp_path, page_size):
    identity = security(db_session)
    value = request(page_size=page_size, page_token="prior_page==")
    payload = {"symbol": "AAPL", "bars": [bar()], "next_page_token": "next_page=="}

    result = storage()(db_session, identity.id, value, response(payload), payload, tmp_path)

    assert result["page_size"] == page_size
    assert result["next_page_token"] == "next_page=="
    assert result["target_start"] == value.start.isoformat()
    assert result["target_end"] == value.end.isoformat()
    assert result["request_anchor"] == value.captured_at.isoformat()
    attempt = db_session.scalar(select(IngestionAttempt).where(IngestionAttempt.content_hash == result["raw_hash"]))
    assert attempt.evidence["params"]["limit"] == str(page_size)
    assert attempt.evidence["params"]["page_token"] == "prior_page=="


def test_rejected_evidence_flush_is_fenced_before_autoflush_and_rolls_back(db_session, tmp_path):
    identity = security(db_session)
    payload = {"symbol": "AAPL", "bars": [bar(c="NaN") for _ in range(13)], "next_page_token": None}
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
        storage()(db_session, identity.id, request(), response(payload), payload, tmp_path, fence=fence)

    assert seen[-2:] == [0, 10]
    assert max(seen) == 10
    assert db_session.scalar(select(func.count()).select_from(ValidationIssue).where(
        ValidationIssue.evidence["security_id"].astext == identity.id)) == 0
    assert db_session.scalar(select(func.count()).select_from(IngestionAttempt)) == prior_attempts
    assert db_session.scalar(select(func.count()).select_from(RawObject)) == prior_raw
