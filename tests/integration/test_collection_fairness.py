from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

from backtest_data.db import utcnow
from backtest_data.ingestion.jobs import acquire_job, plan_job
from backtest_data.ingestion.supervisor import run_collection


def test_fair_lease_does_not_let_a_paged_job_starve_unstarted_targets(db_session):
    paged = plan_job(db_session, source="kis", domain="prices", entity_id="paged",
                     request={"market": "KR"}, priority=1)
    fresh = plan_job(db_session, source="kis", domain="prices", entity_id="fresh",
                     request={"market": "KR"}, priority=10)
    paged.status, paged.attempts = "retry", 30
    db_session.flush()
    lease = acquire_job(db_session, "fair", utcnow(), timedelta(minutes=1),
                        domains=["prices"], sources=["kis"], fair=True)
    assert lease.job_id == fresh.id


def test_empty_source_filter_cannot_claim_any_job(db_session):
    plan_job(db_session, source="sec", domain="filing_sql", entity_id="filing", request={"market": "US"})
    assert acquire_job(db_session, "none", utcnow(), timedelta(minutes=1), sources=[]) is None


def _isolated_collector(db_session, monkeypatch, credentials):
    import backtest_data.ingestion.supervisor as module
    @contextmanager
    def tx(engine):
        yield db_session
    monkeypatch.setattr(module, "transaction", tx)
    monkeypatch.setattr(module, "load_credentials", lambda **kwargs: credentials)
    monkeypatch.setattr(module, "ProviderHTTP", lambda engine: SimpleNamespace(close=lambda: None))
    return module


def test_missing_us_price_provider_preserves_entire_pending_queue(db_session, monkeypatch, tmp_path):
    _isolated_collector(db_session, monkeypatch, SimpleNamespace())
    job = plan_job(db_session, source="alpaca", domain="prices", entity_id="us-target",
                   request={"market": "US"})
    result = run_collection(None, tmp_path, market="us", domain="prices", budget_seconds=1)
    db_session.refresh(job)
    assert job.status == "pending" and job.attempts == 0
    assert result["status"] == "blocked" and result["processed_this_run"] == 0
    assert result["target_scope_preserved"]


def test_one_regulator_rate_limit_does_not_stop_other_regulator(db_session, monkeypatch, tmp_path):
    from backtest_data.ingestion.http import ProviderError
    module = _isolated_collector(db_session, monkeypatch,
        SimpleNamespace(opendart_api_key=True, sec_user_agent="contact@example.test"))
    # This case verifies source isolation, independently of test database IO.
    # Server wall time for the production lease helpers remains real.
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 0.0))
    kr = plan_job(db_session, source="opendart", domain="filing_sql", entity_id="KR-rate",
                  request={"market": "KR", "filing": {}})
    us = plan_job(db_session, source="sec", domain="filing_sql", entity_id="US-ok",
                  request={"market": "US", "filing": {}})
    seen = []
    def collect(engine, request, *, source, **kwargs):
        seen.append(source)
        if source == "opendart":
            raise ProviderError("opendart", "020", retryable=True)
        return {"status": "succeeded", "stored_source_facts": 0}
    monkeypatch.setattr(module, "collect_filing", collect)
    result = run_collection(None, tmp_path, market="both", domain="fundamentals", budget_seconds=1)
    db_session.refresh(kr)
    db_session.refresh(us)
    assert seen == ["opendart", "sec"]
    assert kr.status == "retry" and us.status == "succeeded"
    assert result["blocked_sources"]["opendart"] == "020"


def test_execution_budget_is_not_exhausted_by_reused_planning(db_session,monkeypatch,tmp_path):
    module=_isolated_collector(db_session,monkeypatch,SimpleNamespace(opendart_api_key=True))
    plan_job(db_session,source="opendart",domain="filing_sql",entity_id="ready-after-setup",request={"market":"KR","filing":{}})
    clock,seen=[0.0],[]
    monkeypatch.setattr(module.time,"monotonic",lambda:clock[0])
    def planning(session):
        clock[0]+=2
        return {"created_jobs":0}
    def collect(*args,**kwargs):
        seen.append("collected")
        clock[0]+=2
        return {}
    monkeypatch.setattr(module,"plan_dart_json",planning)
    monkeypatch.setattr(module,"collect_filing",collect)
    result=run_collection(None,tmp_path,market="kr",domain="fundamentals",budget_seconds=1)
    assert seen==["collected"] and result["setup_elapsed_seconds"]==2


def test_filing_priority_cannot_starve_issuer_discovery(db_session, monkeypatch, tmp_path):
    module = _isolated_collector(db_session, monkeypatch, SimpleNamespace(sec_user_agent="contact@example.test"))
    discovery = plan_job(db_session, source="sec", domain="issuer_discovery", entity_id="issuer",
                         request={"market": "US"}, priority=2)
    for index in range(4):
        plan_job(db_session, source="sec", domain="filing_sql", entity_id=f"old-{index}",
                 request={"market": "US", "filing": {}}, priority=1)
    seen = []
    monkeypatch.setattr(module, "discover_issuer", lambda *args, **kwargs: seen.append("discovery") or {})
    monkeypatch.setattr(module, "collect_filing", lambda *args, **kwargs: seen.append("filing") or {})
    run_collection(None, tmp_path, market="us", domain="fundamentals", budget_seconds=1)
    assert seen[0] == "discovery"
    db_session.refresh(discovery)
    assert discovery.status == "succeeded"


def test_bounded_resumptions_do_not_restart_with_same_source(db_session, monkeypatch, tmp_path):
    module = _isolated_collector(db_session, monkeypatch,
        SimpleNamespace(opendart_api_key=True, sec_user_agent="contact@example.test"))
    for index in range(3):
        plan_job(db_session, source="opendart", domain="filing_sql", entity_id=f"KR-slow-{index}",
                 request={"market":"KR", "filing":{}})
    plan_job(db_session, source="sec", domain="filing_sql", entity_id="US-ready",
             request={"market":"US", "filing":{}})
    clock, seen = [0.0], []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    def collect(engine, request, *, source, **kwargs):
        seen.append(source)
        clock[0] += 2
        return {"status":"succeeded"}
    monkeypatch.setattr(module, "collect_filing", collect)
    run_collection(None, tmp_path, market="both", domain="fundamentals", budget_seconds=1)
    run_collection(None, tmp_path, market="both", domain="fundamentals", budget_seconds=1)
    assert seen == ["opendart", "sec"]
