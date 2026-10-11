from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import importlib
import importlib.util
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select, update

from backtest_data.db import new_id, utcnow
from backtest_data.ingestion.http import ProviderError, ProviderResponse
from backtest_data.ingestion.jobs import LeaseLost, plan_job
from backtest_data.ingestion.models import CollectionJob, TargetUniverseManifest


pytestmark = pytest.mark.integration
DAY = date(2026, 10, 7)


def collection():
    assert importlib.util.find_spec("backtest_data.prices.krx_quote_collection") is not None, "KRX whole-range collector is required"
    return importlib.import_module("backtest_data.prices.krx_quote_collection")


def manifest(session, *, start="2016-01-01", end="2026-10-07", markets=None):
    value = TargetUniverseManifest(content_hash=new_id().replace("-", "") * 2,
        scope={"output_start": start, "output_end": end, "markets": markets or ["KR", "US"], "historical_universe_complete": False},
        members=[{"security_id": "irrelevant-current-target", "market": "KR"}], gaps=[])
    session.add(value)
    session.flush()
    return value


def test_whole_range_planning_retains_all_calendar_days_and_all_services(db_session):
    module = collection()
    target = manifest(db_session)
    scope, members = dict(target.scope), list(target.members)
    insert_sizes = []
    def count(connection, cursor, statement, parameters, context, many):
        if context.isinsert and context.compiled.statement.table.name == "collection_job":
            insert_sizes.append(len(parameters))
    event.listen(db_session.bind, "before_cursor_execute", count)
    try:
        result = module.plan_krx_quotes(db_session, target, date(2016, 1, 1), DAY)
    finally:
        event.remove(db_session.bind, "before_cursor_execute", count)
    expected = ((DAY - date(2016, 1, 1)).days + 1) * 10
    assert result["created_jobs"] == expected and result["calendar_days"] * 10 == expected
    assert expected == 39330 and len(module.KRX_SERVICES) == 10
    assert all(size <= 500 * len(CollectionJob.__table__.columns) for size in insert_sizes)
    jobs = db_session.scalars(select(CollectionJob).where(CollectionJob.source == "krx")).all()
    assert len(jobs) == expected
    assert set(job.request["service"] for job in jobs) == set(module.KRX_SERVICES)
    assert all(job.domain == "market_quotes" and job.request["market"] == "KR" for job in jobs)
    assert any(job.request["session_date"] == "2016-01-03" for job in jobs), "An unqualified calendar cannot remove Sunday requests"
    assert target.scope == scope and target.members == members
    assert result["coverage_complete"] is False


def test_replanning_a_new_manifest_reuses_completed_jobs_and_preserves_checkpoints(db_session):
    module = collection()
    first = manifest(db_session)
    module.plan_krx_quotes(db_session, first, DAY, DAY, services=["kospi"])
    job = db_session.scalar(select(CollectionJob).where(CollectionJob.source == "krx"))
    job.status, job.checkpoint = "succeeded", {"raw_id": "preserved", "source_rows_total": 942}
    db_session.flush()
    second = manifest(db_session)
    result = module.plan_krx_quotes(db_session, second, DAY, DAY, services=["kospi"])
    assert result["created_jobs"] == 0 and result["reused_jobs"] == 1
    db_session.refresh(job)
    assert job.status == "succeeded" and job.checkpoint == {"raw_id": "preserved", "source_rows_total": 942}
    assert job.request["manifest_id"] == first.id


@pytest.mark.parametrize("start,end,services", [
    (date(2015, 12, 31), DAY, None), (DAY, date(2026, 10, 8), None), (DAY, date(2026, 10, 6), None),
    (DAY, DAY, ["unauthorized"]), (DAY, DAY, []),
])
def test_planning_rejects_dates_outside_manifest_and_unknown_service(db_session, start, end, services):
    module = collection()
    target = manifest(db_session)
    with pytest.raises(ValueError):
        module.plan_krx_quotes(db_session, target, start, end, **({"services": services} if services is not None else {}))
    assert db_session.scalar(select(func.count()).select_from(CollectionJob).where(CollectionJob.source == "krx")) == 0


def test_planning_rejects_future_local_days_even_with_a_future_manifest(db_session, monkeypatch):
    module = collection()
    monkeypatch.setattr(module, "utcnow", lambda: datetime(2026, 10, 7, 14, tzinfo=timezone.utc))
    target = manifest(db_session, end="2026-10-10")
    with pytest.raises(ValueError, match="future"):
        module.plan_krx_quotes(db_session, target, DAY, date(2026, 10, 8))


def test_planning_rejects_today_without_clipping_the_manifest_scope(db_session, monkeypatch):
    module = collection()
    monkeypatch.setattr(module, "utcnow", lambda: datetime(2026, 10, 7, 14, tzinfo=timezone.utc))
    target = manifest(db_session)
    prior_scope = dict(target.scope)
    with pytest.raises(ValueError, match="completed"):
        module.plan_krx_quotes(db_session, target, DAY, DAY)
    assert target.scope == prior_scope
    assert db_session.scalar(select(func.count()).select_from(CollectionJob).where(CollectionJob.source == "krx")) == 0


def isolated(db_session, monkeypatch, *, credentials=True, quotes=None):
    module = collection()
    import backtest_data.providers as providers
    @contextmanager
    def tx(engine):
        with db_session.begin_nested():
            yield db_session
    monkeypatch.setattr(module, "transaction", tx)
    monkeypatch.setattr(module, "load_credentials", lambda **kwargs: SimpleNamespace(krx_api_key=credentials))
    monkeypatch.setattr(module, "ProviderHTTP", lambda engine: SimpleNamespace(close=lambda: None))
    seen = []
    class KRX:
        def __init__(self, http, credentials):
            pass
        def daily_quotes(self, service, session_date):
            seen.append((service, session_date))
            if quotes:
                return quotes(service, session_date)
            payload = {"OutBlock_1": []}
            return ProviderResponse(json.dumps(payload).encode(), 200, utcnow(), "application/json"), payload
    monkeypatch.setattr(providers, "KRXData", KRX, raising=False)
    return module, seen


def test_runtime_budget_and_service_filter_preserve_pending_queue_and_resume(db_session, monkeypatch, tmp_path):
    module, seen = isolated(db_session, monkeypatch)
    target = manifest(db_session)
    module.plan_krx_quotes(db_session, target, date(2026, 10, 6), DAY, services=["kospi", "etf"])
    other = plan_job(db_session, source="krx", domain="prices", entity_id="excluded-domain", request={"market": "KR"})
    foreign = plan_job(db_session, source="kis", domain="market_quotes", entity_id="excluded-source", request={"market": "KR"})
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    original = module.store_krx_quotes
    def store(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] += 2
        return result
    monkeypatch.setattr(module, "store_krx_quotes", store)
    first = module.run_krx_quote_collection(None, tmp_path, budget_seconds=1, services=["kospi"])
    assert first["status"] == "partial" and first["processed_this_run"] == 1 and first["budget_exhausted"]
    assert first["coverage_complete"] is False and first["target_scope_preserved"]
    second = module.run_krx_quote_collection(None, tmp_path, budget_seconds=1, services=["kospi"])
    assert second["processed_this_run"] == 1
    assert seen == [("kospi", date(2026, 10, 6)), ("kospi", DAY)]
    db_session.refresh(other)
    db_session.refresh(foreign)
    assert other.status == foreign.status == "pending" and other.attempts == foreign.attempts == 0
    etf = db_session.scalars(select(CollectionJob).where(CollectionJob.request["service"].astext == "etf")).all()
    assert all(job.status == "pending" and job.attempts == 0 for job in etf)
    kospi = db_session.scalars(select(CollectionJob).where(CollectionJob.request["service"].astext == "kospi")).all()
    assert all(job.status == "succeeded" and job.checkpoint["empty_response"] for job in kospi)
    assert all(job.checkpoint["official_calendar_verified"] is False for job in kospi)


def test_missing_credentials_do_not_claim_work(db_session, monkeypatch, tmp_path):
    module, seen = isolated(db_session, monkeypatch, credentials=False)
    module.plan_krx_quotes(db_session, manifest(db_session), DAY, DAY)
    result = module.run_krx_quote_collection(None, tmp_path, budget_seconds=1)
    assert result["status"] == "blocked" and result["processed_this_run"] == 0 and not seen
    assert all(job.status == "pending" and job.attempts == 0 for job in db_session.scalars(select(CollectionJob).where(CollectionJob.source == "krx")))


@pytest.mark.parametrize("code,retryable", [("401", False), ("429", True), ("transport_failed", True), ("500", False)])
def test_provider_error_stops_the_source_and_preserves_unclaimed_work(db_session, monkeypatch, tmp_path, code, retryable):
    def quotes(*args):
        raise ProviderError("krx", code, retryable=retryable)
    module, seen = isolated(db_session, monkeypatch, quotes=quotes)
    module.plan_krx_quotes(db_session, manifest(db_session), DAY, DAY)
    result = module.run_krx_quote_collection(None, tmp_path, budget_seconds=1)
    assert len(seen) == result["processed_this_run"] == 1
    assert result["blocked_sources"] == {"krx": code}
    jobs = db_session.scalars(select(CollectionJob).where(CollectionJob.source == "krx")).all()
    assert sum(job.status == "retry" for job in jobs) == 1
    assert sum(job.status == "pending" and job.attempts == 0 for job in jobs) == 9


def test_lease_loss_stops_stage_without_manual_requeue(db_session, monkeypatch, tmp_path):
    module, seen = isolated(db_session, monkeypatch)
    module.plan_krx_quotes(db_session, manifest(db_session), DAY, DAY)
    def lost(*args, **kwargs):
        raise LeaseLost("Expired or superseded lease")
    monkeypatch.setattr(module, "store_krx_quotes", lost)
    result = module.run_krx_quote_collection(None, tmp_path, budget_seconds=1)
    assert len(seen) == 1 and result["blocked_sources"] == {"krx": "lease_lost"}
    jobs = db_session.scalars(select(CollectionJob).where(CollectionJob.source == "krx")).all()
    assert sum(job.status == "running" and job.fencing_token == 1 for job in jobs) == 1
    assert sum(job.status == "pending" and job.attempts == 0 for job in jobs) == 9


def test_five_minute_fencing_prevents_expired_capture_from_entering_sql(db_session, monkeypatch, tmp_path):
    import backtest_data.prices.krx_quote_models as models
    def quotes(*args):
        module_now[0] += timedelta(minutes=6)
        # Live collectors use the server clock. Expire the actual test lease,
        # rather than relying only on an advanced Python fixture timestamp.
        db_session.execute(update(CollectionJob).where(CollectionJob.source == "krx",
            CollectionJob.domain == "market_quotes", CollectionJob.status == "running")
            .values(lease_until=func.clock_timestamp() - timedelta(seconds=1)))
        payload = {"OutBlock_1": [{"BAS_DD": "20261007", "ISU_CD": "095570"}]}
        return ProviderResponse(json.dumps(payload).encode(), 200, utcnow(), "application/json"), payload
    module, seen = isolated(db_session, monkeypatch, quotes=quotes)
    module_now = [utcnow()]
    monkeypatch.setattr(module, "utcnow", lambda: module_now[0])
    module.plan_krx_quotes(db_session, manifest(db_session), DAY, DAY, services=["kospi"])
    result = module.run_krx_quote_collection(None, tmp_path, budget_seconds=1)
    assert result["blocked_sources"] == {"krx": "lease_lost"}
    assert db_session.scalar(select(func.count()).select_from(models.KRXQuoteObservation)) == 0


def test_slow_job_selection_does_not_spend_krx_lease_before_acquisition(db_session, monkeypatch, tmp_path):
    module, seen = isolated(db_session, monkeypatch)
    module.plan_krx_quotes(db_session, manifest(db_session), DAY, DAY, services=["kospi"])
    clock = [utcnow()]
    monkeypatch.setattr(module, "utcnow", lambda: clock[0])
    original = module.acquire_job

    def slow_acquire(session, owner, now, ttl, **kwargs):
        clock[0] += timedelta(minutes=6)
        return original(session, owner, now, ttl, **kwargs)

    monkeypatch.setattr(module, "acquire_job", slow_acquire)
    result = module.run_krx_quote_collection(None, tmp_path, budget_seconds=60, services=["kospi"])
    assert len(seen) == 1 and result["blocked_sources"] == {}
    assert result["request_queue_complete"] is True
