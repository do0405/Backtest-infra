from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest
from sqlalchemy import event, func, select

from backtest_data.catalog.models import Security
from backtest_data.db import new_id
from backtest_data.ingestion.http import ProviderResponse
from backtest_data.ingestion.jobs import JobLease, LeaseLost, complete_job, heartbeat_job, stable_hash
from backtest_data.ingestion.models import CollectionJob, IngestionAttempt, RawObject
from backtest_data.prices.adapters.kis import KISDailyRequest
from backtest_data.prices.models import PriceObservation
from backtest_data.prices.storage import store_kis_prices
from backtest_data.ingestion.raw_store import RawStore
from backtest_data.releases.models import SelectionDecision, ValidationIssue


pytestmark = pytest.mark.integration
CAPTURED = datetime(2026, 10, 9, tzinfo=timezone.utc)
TTL = timedelta(minutes=5)


def setup(session):
    security = Security(market="KR", source_identifier="domestic-fence-test", security_type="unclassified", name="Example")
    session.add(security)
    session.flush()
    job = CollectionJob(identity_hash=stable_hash(["domestic-fence-test", new_id()]), source="kis", domain="prices",
        entity_id=security.id, request={}, checkpoint={}, status="running", lease_owner="test-owner",
        lease_until=CAPTURED + TTL, fencing_token=7, attempts=1)
    session.add(job)
    session.flush()
    return security, job, JobLease(job.id, job.lease_owner, job.fencing_token)


def bar(day="20250113", **changes):
    return {"stck_bsop_date": day, "stck_oprc": "100.0100", "stck_hgpr": "110", "stck_lwpr": "90",
        "stck_clpr": "105", "acml_vol": "100", "acml_tr_pbmn": "10212.5000"} | changes


def store(session, security, root, rows, *, captured=CAPTURED, fence=None, replay_evidence=None):
    payload = {"rt_cd": "0", "output2": rows}
    response = ProviderResponse(json.dumps(payload).encode(), 200, captured, "application/json")
    return store_kis_prices(session, security.id, KISDailyRequest("005930", date(2025, 1, 1), date(2025, 2, 16)),
        response, payload, root, fence=fence, replay_evidence=replay_evidence)


def is_price_read(context):
    statement = getattr(context.compiled, "statement", None)
    return (statement is not None and statement.is_select
        and any(item.get("entity") is PriceObservation for item in statement.column_descriptions))


def test_real_domestic_lease_renews_between_reads_for_page_longer_than_ttl(db_session, tmp_path):
    security, job, lease = setup(db_session)
    clock = [CAPTURED]

    def delay(connection, cursor, statement, parameters, context, many):
        if is_price_read(context):
            clock[0] += timedelta(seconds=160)

    def fence():
        heartbeat_job(db_session, lease, clock[0], TTL)

    rows = [bar("20250114"), bar(stck_oprc="19955", stck_hgpr="19955", stck_lwpr="19955",
        stck_clpr="19955", acml_vol="0", acml_tr_pbmn="0")]
    event.listen(db_session.bind, "after_cursor_execute", delay)
    try:
        result = store(db_session, security, tmp_path, rows, fence=fence)
        complete_job(db_session, lease, clock[0], result, status="retry")
    finally:
        event.remove(db_session.bind, "after_cursor_execute", delay)

    assert clock[0] - CAPTURED == timedelta(seconds=640)
    assert result["inserted"] == result["source_row_count"] == 2
    assert result["cursor_earliest_session"] == "2025-01-13"
    assert result["supplier_zero_transaction_rows"] == 1
    observations = db_session.scalars(select(PriceObservation).where(PriceObservation.security_id == security.id)
        .order_by(PriceObservation.session.desc())).all()
    assert observations[0].open.as_tuple() == Decimal("100.0100").as_tuple()
    assert all(item.available_at == item.captured_at == CAPTURED for item in observations)
    zero = observations[1]
    assert zero.evidence["supplier_reported_zero_transactions"] is True
    assert zero.evidence["no_trade_status_verified"] is False
    decision = db_session.scalar(select(SelectionDecision).where(SelectionDecision.record_id == zero.id))
    assert decision.evidence_grade == "supplier_reported_unverified"
    db_session.refresh(job)
    assert job.status == "retry" and job.checkpoint["inserted"] == 2
    assert store(db_session, security, tmp_path, rows,
        replay_evidence={"original_ingestion_attempt_id": result["ingestion_attempt_id"]})["inserted"] == 0
    db_session.refresh(zero)
    assert "replay_evidence" not in zero.evidence


@pytest.mark.parametrize("phase", ["prior_read", "price_insert", "decision_flush", "issue_flush"])
def test_real_domestic_expiry_rolls_back_page_and_preserves_existing_capture(db_session, tmp_path, phase):
    security, job, lease = setup(db_session)
    store(db_session, security, tmp_path, [bar()], captured=CAPTURED - timedelta(days=1))
    old = db_session.scalar(select(PriceObservation).where(PriceObservation.security_id == security.id))
    original = (old.id, old.signature, deepcopy(old.evidence), old.open.as_tuple())
    counts = {model: db_session.scalar(select(func.count()).select_from(model))
        for model in (RawObject, IngestionAttempt, SelectionDecision, ValidationIssue)}
    clock = [CAPTURED]

    def delay_query(connection, cursor, statement, parameters, context, many):
        if (phase == "prior_read" and is_price_read(context)
                or phase == "price_insert" and context.isinsert
                and context.compiled.statement.table.name == "price_observation"):
            clock[0] += TTL + timedelta(seconds=1)

    def delay_flush(session, context, instances):
        target = SelectionDecision if phase == "decision_flush" else ValidationIssue if phase == "issue_flush" else None
        if target is not None and any(isinstance(item, target) for item in session.new):
            clock[0] += TTL + timedelta(seconds=1)

    def fence():
        heartbeat_job(db_session, lease, clock[0], TTL)

    row = bar(stck_clpr="NaN" if phase == "issue_flush" else "106")
    event.listen(db_session.bind, "after_cursor_execute", delay_query)
    event.listen(db_session, "before_flush", delay_flush)
    try:
        with pytest.raises(LeaseLost), db_session.begin_nested():
            store(db_session, security, tmp_path, [row], fence=fence)
    finally:
        event.remove(db_session.bind, "after_cursor_execute", delay_query)
        event.remove(db_session, "before_flush", delay_flush)

    db_session.refresh(old)
    db_session.refresh(job)
    assert (old.id, old.signature, old.evidence, old.open.as_tuple()) == original
    assert db_session.scalar(select(func.count()).select_from(PriceObservation).where(
        PriceObservation.security_id == security.id)) == 1
    assert all(db_session.scalar(select(func.count()).select_from(model)) == count for model, count in counts.items())
    assert job.status == "running" and job.fencing_token == 7 and job.lease_until == CAPTURED + TTL


def test_real_domestic_rejection_flush_checks_owner_before_autoflush(db_session, tmp_path):
    security, job, lease = setup(db_session)
    seen = []

    def fence():
        count = db_session.scalar(select(func.count()).select_from(ValidationIssue).where(
            ValidationIssue.evidence["security_id"].astext == security.id))
        seen.append(count)
        if count:
            raise LeaseLost("Expired or superseded lease")

    rows = [bar((date(2025, 1, 13) - timedelta(days=index)).strftime("%Y%m%d"), stck_clpr="NaN")
        for index in range(13)]
    with pytest.raises(LeaseLost), db_session.begin_nested():
        store(db_session, security, tmp_path, rows, fence=fence)

    assert seen[-2:] == [0, 10]
    assert max(seen) == 10
    assert db_session.scalar(select(func.count()).select_from(ValidationIssue).where(
        ValidationIssue.evidence["security_id"].astext == security.id)) == 0


@pytest.mark.parametrize("mode", ["db_metadata", "archive_put"])
def test_real_domestic_metadata_steps_keep_valid_owner_between_stalls(db_session, tmp_path, monkeypatch, mode):
    security, job, lease = setup(db_session)
    clock = [CAPTURED]

    def delay(connection, cursor, statement, parameters, context, many):
        if context.isinsert:
            table = context.compiled.statement.table.name
            if table == "source_contract" or mode == "db_metadata" and table == "raw_object":
                clock[0] += timedelta(seconds=160)
        compiled = getattr(context.compiled, "statement", None)
        if (mode == "db_metadata" and compiled is not None and compiled.is_select
                and any(item.get("entity") is RawObject for item in compiled.column_descriptions)):
            clock[0] += timedelta(seconds=160)

    if mode == "archive_put":
        original_put = RawStore.put

        def delayed_put(archive, content):
            result = original_put(archive, content)
            clock[0] += timedelta(seconds=160)
            return result

        monkeypatch.setattr(RawStore, "put", delayed_put)

    def fence():
        heartbeat_job(db_session, lease, clock[0], TTL)

    event.listen(db_session.bind, "after_cursor_execute", delay)
    try:
        result = store(db_session, security, tmp_path, [bar()], fence=fence)
        complete_job(db_session, lease, clock[0], result)
    finally:
        event.remove(db_session.bind, "after_cursor_execute", delay)

    assert result["inserted"] == 1
    assert clock[0] - CAPTURED == timedelta(seconds=480 if mode == "db_metadata" else 320)
    db_session.refresh(job)
    assert job.status == "succeeded" and job.checkpoint["inserted"] == 1
