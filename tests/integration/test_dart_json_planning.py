import json
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.dialects.postgresql import insert

from backtest_data.catalog.models import Issuer
from backtest_data.db import new_id, utcnow
from backtest_data.fundamentals.dart_json_collection import collect_dart_json, plan_dart_json
from backtest_data.fundamentals.models import Filing, SourceFinancialFact
from backtest_data.ingestion.http import ProviderResponse
from backtest_data.ingestion.jobs import plan_job
from backtest_data.ingestion.models import CollectionJob


pytestmark = pytest.mark.integration
RECEIPT = "20260320000123"
TITLE = "사업보고서 (2025.12)"


def issuer(session, *, code="00123456", market="KR"):
    value = Issuer(market=market, source_identifier="DART:" + code, name="Example")
    session.add(value)
    session.flush()
    return value


def discovered(session, identity, *, receipt=RECEIPT, title=TITLE, **changes):
    request = {"market": "KR", "issuer_id": identity.id, "disclosure_profile": "periodic_report",
        "filing": {"rcept_no": receipt, "report_nm": title, "corp_code": identity.source_identifier[5:]},
        "reprt_code": None, "reprt_code_candidates": ["11013", "11014"],
        "output_start": "2016-01-01", "output_end": "2026-10-07"}
    request.update(changes.pop("request", {}))
    job = CollectionJob(identity_hash=new_id().replace("-", "") * 2, source="opendart", domain="filing_sql",
        entity_id=receipt, request=request, **changes)
    session.add(job)
    session.flush()
    return job


def json_jobs(session):
    return session.scalars(select(CollectionJob).where(CollectionJob.source == "opendart",
        CollectionJob.domain == "filing_json")).all()


def stored(session, identity, *, receipt=RECEIPT, title=TITLE):
    value = Filing(issuer_id=identity.id, source="opendart", accession=receipt, form=title,
        available_at=utcnow(), captured_at=utcnow(), evidence={})
    session.add(value)
    session.flush()
    return value


def test_discovered_filings_plan_both_json_scopes_before_xbrl_storage(db_session):
    identity = issuer(db_session)
    original = discovered(db_session, identity)
    result = plan_dart_json(db_session)
    jobs = json_jobs(db_session)
    assert result["created_jobs"] == 2 and result["eligible_filings"] == 1
    assert result["scanned_discovery_jobs"] == 1 and result["scanned_filing_records"] == 0
    assert {job.request["fs_div"] for job in jobs} == {"CFS", "OFS"}
    assert all(job.request["issuer_id"] == identity.id and job.request["receipt"] == RECEIPT for job in jobs)
    assert all(job.request["bsns_year"] == "2025" and job.request["reprt_code_candidates"] == ["11011"] for job in jobs)
    assert all("period_end" not in job.request for job in jobs)
    assert db_session.scalar(select(func.count()).select_from(Filing)) == 0
    assert original.status == "pending"


def test_stored_discovered_and_duplicate_discovery_rows_reuse_the_same_receipt_work(db_session):
    identity = issuer(db_session)
    stored(db_session, identity)
    discovered(db_session, identity, request={"manifest_id": "one"})
    discovered(db_session, identity, request={"manifest_id": "two"})
    first = plan_dart_json(db_session)
    jobs = json_jobs(db_session)
    jobs[0].status, jobs[0].checkpoint = "unsupported", {"reason": "latest_only_receipt_mismatch"}
    db_session.flush()
    second = plan_dart_json(db_session)
    assert first["created_jobs"] == 2 and first["eligible_filings"] == 1
    assert first["duplicate_records"] == 2
    assert second["created_jobs"] == 0 and second["reused_jobs"] == 2
    assert len(json_jobs(db_session)) == 2
    assert jobs[0].status == "unsupported" and jobs[0].checkpoint["reason"] == "latest_only_receipt_mismatch"


@pytest.mark.parametrize("changes", [
    {"receipt": "2026-03-20"}, {"receipt": "２０２６０３２００００１２３"},
    {"request": {"market": "US"}}, {"request": {"issuer_id": "missing"}},
    {"request": {"issuer_id": None}}, {"request": {"filing": {"rcept_no": RECEIPT, "report_nm": TITLE, "corp_code": "00888888"}}},
    {"title": "not a periodic filing (2025.12)"}, {"title": "사업보고서 (2025.13)"},
    {"title": "사업보고서 (2025.12) (2024.12)"},
])
def test_invalid_discovery_identity_or_title_never_plans_json_work(db_session, changes):
    identity = issuer(db_session)
    discovered(db_session, identity, **changes)
    result = plan_dart_json(db_session)
    assert result["created_jobs"] == 0 and result["invalid_records"] == 1
    assert json_jobs(db_session) == []


def test_issuer_market_is_checked_even_when_dart_identifier_looks_valid(db_session):
    identity = issuer(db_session, market="US")
    discovered(db_session, identity)
    result = plan_dart_json(db_session)
    assert result["invalid_records"] == 1 and json_jobs(db_session) == []


@pytest.mark.parametrize("filing", [None, [], {"rcept_no": True, "report_nm": TITLE}, {"rcept_no": RECEIPT}])
def test_malformed_nested_discovery_metadata_is_counted_without_crashing(db_session, filing):
    identity = issuer(db_session)
    discovered(db_session, identity, request={"filing": filing})
    result = plan_dart_json(db_session)
    assert result["invalid_records"] == 1 and result["created_jobs"] == 0


def test_discovery_job_entity_must_equal_its_receipt(db_session):
    identity = issuer(db_session)
    job = discovered(db_session, identity)
    job.entity_id = "20260320000999"
    db_session.flush()
    result = plan_dart_json(db_session)
    assert result["invalid_records"] == 1 and json_jobs(db_session) == []


def test_minimal_exact_discovery_metadata_uses_verified_issuer_corporation_id(db_session):
    identity = issuer(db_session)
    discovered(db_session, identity, request={"filing": {"rcept_no": RECEIPT, "report_nm": TITLE}})
    result = plan_dart_json(db_session)
    assert result["created_jobs"] == 2
    assert all(job.request["corp_code"] == "00123456" for job in json_jobs(db_session))


@pytest.mark.parametrize("conflict", ["issuer", "title"])
def test_ambiguous_receipt_identity_fails_closed_before_any_json_jobs(db_session, conflict):
    first = issuer(db_session)
    stored(db_session, first)
    other = issuer(db_session, code="00222222") if conflict == "issuer" else first
    discovered(db_session, other, title="반기보고서 (2025.06)" if conflict == "title" else TITLE)
    result = plan_dart_json(db_session)
    assert result["ambiguous_receipts"] == 1 and result["eligible_filings"] == 0
    assert json_jobs(db_session) == []


def test_title_year_is_exploratory_and_quarter_candidates_do_not_infer_fiscal_ordinal(db_session):
    identity = issuer(db_session)
    discovered(db_session, identity, title="분기보고서 (2025.03)")
    discovered(db_session, identity, receipt="20140320000123", title="사업보고서 (2013.12)")
    result = plan_dart_json(db_session)
    assert result["unsupported_period_records"] == 1 and result["created_jobs"] == 2
    assert all(job.request["reprt_code_candidates"] == ["11013", "11014"] for job in json_jobs(db_session))
    assert result["historical_date_qualification"] is False


def test_existing_logical_request_with_manifest_binding_is_reused(db_session):
    identity = issuer(db_session)
    discovered(db_session, identity)
    for scope in ("CFS", "OFS"):
        request = {"market": "KR", "issuer_id": identity.id, "receipt": RECEIPT,
            "corp_code": "00123456", "form": TITLE, "bsns_year": "2025",
            "reprt_code_candidates": ["11011"], "fs_div": scope, "disclosure_profile": "periodic_report",
            "manifest_id": "earlier"}
        plan_job(db_session, source="opendart", domain="filing_json", entity_id=RECEIPT + ":" + scope, request=request)
    result = plan_dart_json(db_session)
    assert result["created_jobs"] == 0 and result["reused_jobs"] == 2
    assert len(json_jobs(db_session)) == 2


def test_prior_correction_receipt_still_rejects_latest_response_after_discovery_planning(db_session, monkeypatch):
    import backtest_data.fundamentals.dart_json_collection as module
    identity = issuer(db_session)
    old = discovered(db_session, identity)
    latest = "20260401000123"
    discovered(db_session, identity, receipt=latest, title="[기재정정]" + TITLE)
    assert plan_dart_json(db_session)["created_jobs"] == 4
    request = next(job.request for job in json_jobs(db_session) if job.request["receipt"] == RECEIPT)
    data = {"status": "000", "list": [{"rcept_no": latest, "corp_code": "00123456",
        "reprt_code": "11011", "bsns_year": "2025", "account_id": "ifrs-full_Revenue",
        "sj_div": "IS", "currency": "KRW", "thstrm_amount": "500"}]}
    response = ProviderResponse(json.dumps(data).encode(), 200, utcnow(), "application/json")
    result = collect_dart_json(None, request, dart=SimpleNamespace(fetch=lambda *a, **k: (response, data)))
    assert result["status"] == "unsupported" and result["reason"] == "latest_only_receipt_mismatch"
    assert db_session.scalar(select(func.count()).select_from(SourceFinancialFact)) == 0
    assert old.status == "pending"


def test_large_discovered_queue_planning_uses_batched_queries(db_session):
    identity = issuer(db_session)
    values = []
    for index in range(1250):
        receipt = "20260320" + str(index).zfill(6)
        values.append({"id": new_id(), "identity_hash": new_id().replace("-", "") * 2,
            "source": "opendart", "domain": "filing_sql", "entity_id": receipt,
            "request": {"market": "KR", "issuer_id": identity.id,
                "filing": {"rcept_no": receipt, "report_nm": TITLE, "corp_code": "00123456"}}})
    for offset in range(0, len(values), 500):
        db_session.execute(insert(CollectionJob).values(values[offset:offset + 500]))
    queries = []
    def count_query(connection, cursor, statement, params, context, many):
        queries.append(statement)
    event.listen(db_session.bind, "before_cursor_execute", count_query)
    try:
        first = plan_dart_json(db_session)
    finally:
        event.remove(db_session.bind, "before_cursor_execute", count_query)
    assert first["created_jobs"] == 2500 and first["eligible_filings"] == 1250
    assert len(queries) < 20
