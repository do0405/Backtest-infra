"""Use real SQL ownership checks around foreign price-page metadata and reads."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json

import pytest
from sqlalchemy import event, func, select

from backtest_data.catalog.models import Security
from backtest_data.db import new_id
from backtest_data.ingestion.http import ProviderResponse
from backtest_data.ingestion.jobs import JobLease, LeaseLost, complete_job, heartbeat_job, stable_hash
from backtest_data.ingestion.models import CollectionJob, IngestionAttempt, RawObject
from backtest_data.ingestion.raw_store import RawStore
from backtest_data.prices.adapters.alpaca import AlpacaDailyRequest
from backtest_data.prices.adapters.kis_overseas import KISOverseasDailyRequest
from backtest_data.prices.alpaca_storage import store_alpaca_prices
from backtest_data.prices.kis_overseas_storage import store_kis_overseas_prices
from backtest_data.prices.models import PriceObservation
from backtest_data.releases.models import SelectionDecision, ValidationIssue


pytestmark = pytest.mark.integration
CAPTURED = datetime(2024, 7, 10, 18, tzinfo=timezone.utc)
TTL = timedelta(minutes=5)


def setup(session, provider):
    security = Security(market="US", source_identifier="nasdaq_directory:AAPL", security_type="unclassified", name="Example")
    session.add(security)
    session.flush()
    job = CollectionJob(identity_hash=stable_hash([provider, new_id()]), source=provider, domain="prices",
        entity_id=security.id, request={}, checkpoint={}, status="running", lease_owner="test-owner",
        lease_until=CAPTURED + TTL, fencing_token=7, attempts=1)
    session.add(job)
    session.flush()
    return security, job, JobLease(job.id, job.lease_owner, job.fencing_token)


def page(provider, security, *, rejection=False):
    if provider == "alpaca":
        request = AlpacaDailyRequest(date(2024, 1, 1), date(2024, 7, 9), symbol="AAPL", captured_at=CAPTURED)
        rows = [{"t": "2024-07-09T00:00:00-04:00", "o": "100.0100", "h": "110", "l": "90", "c": "105", "v": 100}]
        if rejection:
            rows.insert(0, rows[0] | {"t": "2024-07-08T00:00:00-04:00", "c": "NaN"})
        return store_alpaca_prices, request, {"symbol": "AAPL", "bars": rows, "next_page_token": None}, {}
    request = KISOverseasDailyRequest("AAPL", date(2024, 1, 1), date(2024, 7, 9), "NAS", captured_at=CAPTURED)
    rows = [{"xymd": "20240709", "open": "100.0100", "high": "110", "low": "90", "clos": "105", "tvol": "100", "tamt": "10212.5000"}]
    if rejection:
        rows.append(rows[0] | {"xymd": "20240708", "clos": "NaN"})
    mapping = {"market": "US", "security_id": security.id, "provider_symbol": "AAPL", "exchange": "NAS",
        "source": "kis_overseas_master", "qualified_current_mapping": True, "source_directory_hash": "a" * 64,
        "realtime_symbol": "DNASAAPL"}
    return store_kis_overseas_prices, request, {"rt_cd": "0", "output1": {"rsym": "DNASAAPL", "zdiv": "4"},
        "output2": rows}, {"mapping_evidence": mapping}


def store_page(session, provider, security, root, *, fence=None, rejection=False):
    store, request, payload, arguments = page(provider, security, rejection=rejection)
    response = ProviderResponse(json.dumps(payload).encode(), 200, CAPTURED, "application/json")
    result = store(session, security.id, request, response, payload, root, fence=fence, **arguments)
    assert result["raw_hash"] == sha256(response.content).hexdigest()
    return result


@pytest.mark.parametrize("provider", ["alpaca", "kis_overseas"])
@pytest.mark.parametrize("mode", ["db_metadata", "archive_put"])
def test_metadata_renews_valid_owner_between_individually_slow_operations(db_session, tmp_path, monkeypatch, provider, mode):
    security, job, lease = setup(db_session, provider)
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
        result = store_page(db_session, provider, security, tmp_path, fence=fence)
        complete_job(db_session, lease, clock[0], result)
    finally:
        event.remove(db_session.bind, "after_cursor_execute", delay)

    assert clock[0] - CAPTURED == timedelta(seconds=480 if mode == "db_metadata" else 320)
    assert result["inserted"] == 1
    observation = db_session.scalar(select(PriceObservation).where(PriceObservation.security_id == security.id))
    original = (observation.id, observation.signature, dict(observation.evidence), observation.open.as_tuple())
    assert observation.open.as_tuple() == Decimal("100.0100").as_tuple()
    assert observation.available_at == observation.captured_at == CAPTURED
    assert store_page(db_session, provider, security, tmp_path)["inserted"] == 0
    db_session.refresh(observation)
    assert (observation.id, observation.signature, observation.evidence, observation.open.as_tuple()) == original
    db_session.refresh(job)
    assert job.status == "succeeded" and job.checkpoint["inserted"] == 1


@pytest.mark.parametrize("provider", ["alpaca", "kis_overseas"])
def test_expired_metadata_step_rolls_back_before_archive_or_price_write(db_session, tmp_path, provider):
    security, job, lease = setup(db_session, provider)
    counts = {model: db_session.scalar(select(func.count()).select_from(model))
        for model in (RawObject, IngestionAttempt, PriceObservation, SelectionDecision, ValidationIssue)}
    clock = [CAPTURED]

    def delay(connection, cursor, statement, parameters, context, many):
        if context.isinsert and context.compiled.statement.table.name == "source_contract":
            clock[0] += TTL + timedelta(seconds=1)

    def fence():
        heartbeat_job(db_session, lease, clock[0], TTL)

    event.listen(db_session.bind, "after_cursor_execute", delay)
    try:
        with pytest.raises(LeaseLost), db_session.begin_nested():
            store_page(db_session, provider, security, tmp_path, fence=fence)
    finally:
        event.remove(db_session.bind, "after_cursor_execute", delay)

    assert not list(tmp_path.rglob("*.gz"))
    assert all(db_session.scalar(select(func.count()).select_from(model)) == count for model, count in counts.items())
    db_session.refresh(job)
    assert job.status == "running" and job.fencing_token == 7 and job.lease_until == CAPTURED + TTL


@pytest.mark.parametrize("provider", ["alpaca", "kis_overseas"])
def test_pending_rejection_does_not_autoflush_in_heartbeat_or_prior_read(db_session, tmp_path, provider):
    security, job, lease = setup(db_session, provider)
    clock = [CAPTURED]

    def delay_read(connection, cursor, statement, parameters, context, many):
        compiled = getattr(context.compiled, "statement", None)
        if (compiled is not None and compiled.is_select
                and any(item.get("entity") is PriceObservation for item in compiled.column_descriptions)):
            clock[0] += timedelta(seconds=290)

    def delay_issue(session, context, instances):
        if any(isinstance(item, ValidationIssue) for item in session.new):
            clock[0] += timedelta(seconds=10)

    def fence():
        heartbeat_job(db_session, lease, clock[0], TTL)

    event.listen(db_session.bind, "after_cursor_execute", delay_read)
    event.listen(db_session, "before_flush", delay_issue)
    try:
        result = store_page(db_session, provider, security, tmp_path, fence=fence, rejection=True)
        complete_job(db_session, lease, clock[0], result)
    finally:
        event.remove(db_session.bind, "after_cursor_execute", delay_read)
        event.remove(db_session, "before_flush", delay_issue)

    assert clock[0] - CAPTURED == timedelta(seconds=300)
    assert result["inserted"] == result["rejected"] == 1
    db_session.refresh(job)
    assert job.status == "succeeded" and job.checkpoint["rejected"] == 1
