from contextlib import contextmanager
from types import SimpleNamespace
import json

import pytest
from sqlalchemy import func, select

from backtest_data.catalog.models import Issuer
from backtest_data.db import utcnow
from backtest_data.fundamentals.models import Filing, FinancialSourceDocument, SourceFinancialFact
from backtest_data.ingestion.http import ProviderResponse
from backtest_data.ingestion.models import CollectionJob


def fixture_filing(session):
    now = utcnow()
    issuer = Issuer(market="KR", source_identifier="DART:00123456", name="Company")
    session.add(issuer)
    session.flush()
    filing = Filing(issuer_id=issuer.id, source="opendart", accession="20260320000123",
        form="사업보고서 (2025.12)", available_at=now, captured_at=now, evidence={})
    session.add(filing)
    session.flush()
    return issuer, filing


def test_planning_covers_both_scopes_and_is_idempotent(db_session):
    from backtest_data.fundamentals.dart_json_collection import plan_dart_json
    issuer, filing = fixture_filing(db_session)
    first = plan_dart_json(db_session)
    second = plan_dart_json(db_session)
    jobs = db_session.scalars(select(CollectionJob).where(CollectionJob.domain == "filing_json")).all()
    assert first["created_jobs"] == 2 and second["created_jobs"] == 0
    assert {job.request["fs_div"] for job in jobs} == {"CFS", "OFS"}
    assert {job.request["receipt"] for job in jobs} == {filing.accession}
    assert all(job.request["bsns_year"] == "2025" for job in jobs)


def isolate(db_session, monkeypatch):
    import backtest_data.fundamentals.dart_json_collection as module
    @contextmanager
    def tx(engine):
        yield db_session
    monkeypatch.setattr(module, "transaction", tx)
    return module


def test_latest_only_response_is_retained_as_gap_without_old_numeric_rows(db_session, monkeypatch):
    module = isolate(db_session, monkeypatch)
    issuer, filing = fixture_filing(db_session)
    module.plan_dart_json(db_session)
    job = db_session.scalar(select(CollectionJob).where(CollectionJob.domain == "filing_json"))
    data = {"status": "000", "list": [{"rcept_no": "20260401000123", "corp_code": "00123456",
        "reprt_code": "11011", "bsns_year": "2025", "account_id": "ifrs-full_Revenue",
        "sj_div": "IS", "currency": "KRW", "thstrm_amount": "500"}]}
    response = ProviderResponse(json.dumps(data).encode(), 200, utcnow(), "application/json")
    result = module.collect_dart_json(None, job.request, dart=SimpleNamespace(fetch=lambda *a, **k: (response, data)))
    assert result["status"] == "unsupported" and result["reason"] == "latest_only_receipt_mismatch"
    assert db_session.scalar(select(func.count()).select_from(SourceFinancialFact)) == 0


@pytest.mark.parametrize("fenced", [False, True])
def test_matching_json_is_sql_only_scoped_and_preserves_unknown_dates(db_session, monkeypatch, tmp_path, fenced):
    module = isolate(db_session, monkeypatch)
    issuer, filing = fixture_filing(db_session)
    module.plan_dart_json(db_session)
    job = db_session.scalar(select(CollectionJob).where(CollectionJob.domain == "filing_json",
        CollectionJob.request["fs_div"].astext == "CFS"))
    data = {"status": "000", "list": [{"rcept_no": filing.accession, "corp_code": "00123456",
        "reprt_code": "11011", "bsns_year": "2025", "account_id": "ifrs-full_Revenue",
        "sj_div": "IS", "currency": "KRW", "thstrm_amount": "500", "frmtrm_amount": "400"}]}
    response = ProviderResponse(json.dumps(data).encode(), 200, utcnow(), "application/json")
    client = SimpleNamespace(base="https://opendart.fss.or.kr/api/", fetch=lambda *a, **k: (response, data))
    calls = []
    fence = (lambda session: calls.append(session)) if fenced else None
    first = module.collect_dart_json(None, job.request, dart=client, fence=fence)
    module.collect_dart_json(None, job.request, dart=client, fence=fence)
    facts = db_session.scalars(select(SourceFinancialFact)).all()
    documents = db_session.scalars(select(FinancialSourceDocument)).all()
    assert first["status"] == "succeeded" and len(facts) == 2 and len(documents) == 1
    assert all(fact.scope == "consolidated" and fact.period_end is None for fact in facts)
    assert documents[0].inventory["issuer_identity"]["status"] == "matched"
    if fenced:
        assert len(calls) > 4 and all(session is db_session for session in calls)
    assert not list(tmp_path.iterdir())


def test_wrong_report_code_cannot_be_used_as_requested_fiscal_report(db_session, monkeypatch):
    module = isolate(db_session, monkeypatch)
    issuer, filing = fixture_filing(db_session)
    module.plan_dart_json(db_session)
    job = db_session.scalar(select(CollectionJob).where(CollectionJob.domain == "filing_json"))
    data = {"status": "000", "list": [{"rcept_no": filing.accession, "corp_code": "00123456",
        "reprt_code": "11013", "bsns_year": "2025", "account_id": "ifrs-full_Revenue",
        "sj_div": "IS", "currency": "KRW", "thstrm_amount": "500"}]}
    response = ProviderResponse(json.dumps(data).encode(), 200, utcnow(), "application/json")
    result = module.collect_dart_json(None, job.request, dart=SimpleNamespace(fetch=lambda *a, **k: (response, data)))
    assert result["status"] == "unsupported" and result["reason"] == "report_identity_mismatch"
    assert db_session.scalar(select(func.count()).select_from(SourceFinancialFact)) == 0
