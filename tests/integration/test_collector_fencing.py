from datetime import timedelta
import io
from types import SimpleNamespace
import zipfile

import pytest
from sqlalchemy import func, select

from backtest_data.catalog.models import Issuer
from backtest_data.db import utcnow
from backtest_data.fundamentals.collection import collect_filing, dart_report_code
from backtest_data.fundamentals.models import FinancialSourceDocument
from backtest_data.ingestion.jobs import acquire_job, heartbeat_job, plan_job
from backtest_data.ingestion.models import CollectionJob


def test_report_code_never_assumes_calendar_year_quarters():
    assert dart_report_code("사업보고서 (2025.12)") == "11011"
    assert dart_report_code("반기보고서 (2026.06)") == "11012"
    assert dart_report_code("분기보고서 (2026.03)") is None
    assert dart_report_code("분기보고서 (2026.09)") is None


def test_domain_filter_and_expired_fence(db_session):
    now = utcnow()
    plan_job(db_session, source="kis", domain="prices", entity_id="x", request={"market": "KR"})
    filing = plan_job(db_session, source="sec", domain="filing_sql", entity_id="y", request={"market": "US"})
    lease = acquire_job(db_session, "test", now, timedelta(seconds=2), domains=["filing_sql"], market="us")
    assert lease.job_id == filing.id
    with pytest.raises(ValueError, match="Expired"):
        heartbeat_job(db_session, lease, now + timedelta(seconds=3), timedelta(minutes=5))


def test_financial_commit_calls_fence_and_keeps_original_zip_hash(db_session, monkeypatch):
    from contextlib import contextmanager
    import backtest_data.fundamentals.collection as module
    issuer = Issuer(market="KR", source_identifier="DART:00000001", name="test")
    db_session.add(issuer)
    db_session.flush()
    xml = b'''<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:t="urn:test"><context id="c"><entity><identifier scheme="urn:id">00000001</identifier></entity><period><instant>2025-12-31</instant></period></context><unit id="u"><measure>t:KRW</measure></unit><t:Assets contextRef="c" unitRef="u" decimals="0">123</t:Assets></xbrl>'''
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as archive:
        archive.writestr("report.xbrl", xml)
    content = blob.getvalue()
    class Dart:
        base = "https://opendart.fss.or.kr/api/"
        def fetch(self, *args, **kwargs):
            return SimpleNamespace(content=content, captured_at=utcnow())
    @contextmanager
    def tx(engine):
        yield db_session
    monkeypatch.setattr(module, "transaction", tx)
    calls = []
    result = collect_filing(None, {"issuer_id": issuer.id, "reprt_code": "11011", "filing": {"rcept_no": "20260301000001", "report_nm": "사업보고서"}, "disclosure_profile": "periodic_report"}, source="opendart", dart=Dart(), sec=None, fence=lambda session: calls.append(session))
    assert result["stored_source_facts"] == 1
    assert len(calls) > 2 and all(session is db_session for session in calls)
    import hashlib
    document = db_session.scalar(select(FinancialSourceDocument))
    assert document.content_hash == hashlib.sha256(content).hexdigest()
    assert document.instance_key == "0"
