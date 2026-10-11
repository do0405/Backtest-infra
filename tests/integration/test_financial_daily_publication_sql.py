from datetime import datetime, timezone
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
import pytest

from backtest_data.catalog.models import Issuer
from backtest_data.fundamentals.models import Filing, FinancialSourceDocument
from backtest_data.fundamentals.daily_publication import derive_daily_publication, append_daily_publication_evidence
from backtest_data.fundamentals.daily_publication_models import FinancialDailyPublicationEvidence

pytestmark = pytest.mark.integration
CAPTURE = datetime(2026, 10, 10, tzinfo=timezone.utc)


def stored(session, filing_evidence=None):
    issuer = Issuer(market="US", source_identifier="CIK0000000001", name="Daily metadata test")
    session.add(issuer); session.flush()
    filing = Filing(issuer_id=issuer.id, source="sec", accession="0000000001-24-000001", form="10-K",
        available_at=CAPTURE, captured_at=CAPTURE, evidence=filing_evidence or {})
    session.add(filing); session.flush()
    document = FinancialSourceDocument(filing_id=filing.id, content_hash="a" * 64,
        source_url="https://www.sec.gov/metadata", available_at=CAPTURE, captured_at=CAPTURE,
        extraction_complete=True, inventory={}, parser_version="metadata_fixture", instance_key="0")
    session.add(document); session.flush()
    return document, filing


def test_daily_metadata_append_is_idempotent_without_archive_and_preserves_source_clocks(db_session):
    document, filing = stored(db_session)
    before = (document.available_at, document.captured_at, filing.available_at, filing.captured_at, filing.published_at)
    resolution = derive_daily_publication(document, filing, market="US", metadata_records=[
        {"accessionNumber": filing.accession, "filingDate": "2024-02-02"}])
    first = append_daily_publication_evidence(db_session, document=document, filing=filing,
        resolution=resolution, captured_at=CAPTURE, recorded_at=CAPTURE)
    second = append_daily_publication_evidence(db_session, document=document, filing=filing,
        resolution=resolution, captured_at=CAPTURE, recorded_at=CAPTURE)
    assert first.id == second.id
    assert db_session.scalar(select(func.count()).select_from(FinancialDailyPublicationEvidence)) == 1
    assert first.evidence_grade == "metadata_grade"
    assert first.source_fact_id is None
    assert (document.available_at, document.captured_at, filing.available_at, filing.captured_at, filing.published_at) == before


@pytest.mark.parametrize("statement", ["UPDATE financial_daily_publication_evidence SET public_day='2000-01-01' WHERE id=:id",
                                      "DELETE FROM financial_daily_publication_evidence WHERE id=:id"])
def test_daily_metadata_evidence_is_immutable_sql(db_session, statement):
    document, filing = stored(db_session)
    resolution = derive_daily_publication(document, filing, market="US", metadata_records=[
        {"accessionNumber": filing.accession, "filingDate": "2024-02-02"}])
    row = append_daily_publication_evidence(db_session, document=document, filing=filing,
        resolution=resolution, captured_at=CAPTURE, recorded_at=CAPTURE)
    with pytest.raises(DBAPIError, match="immutable"):
        with db_session.begin_nested():
            db_session.execute(text(statement), {"id": row.id})


def test_daily_v8_materialization_uses_metadata_bound_without_strict_certificate(db_session):
    from datetime import date
    from decimal import Decimal
    from backtest_data.fundamentals.materialize import materialize_metrics
    from backtest_data.fundamentals.models import FundamentalFact, SourceFinancialFact
    from backtest_data.releases.models import SelectionDecision
    document, filing = stored(db_session)
    source = SourceFinancialFact(document_id=document.id, issuer_id=filing.issuer_id, concept="Assets", account_name="Assets",
        context_id="exact-context", dimensions={}, period_kind="instant", period_start=None, period_end=date(2023, 12, 31),
        instant=date(2023, 12, 31), scope="consolidated", unit="USD", original_unit="USD", value=Decimal(100), raw_value="100",
        is_nil=False, scale=None, decimals="0", available_at=CAPTURE, captured_at=CAPTURE, extracted_at=CAPTURE,
        first_seen_at=CAPTURE, signature="b" * 64, evidence={"parse_status": "ok"})
    db_session.add(source); db_session.flush()
    canonical = FundamentalFact(issuer_id=filing.issuer_id, source_fact_id=source.id, metric_id="assets_total",
        mapping_version="daily-test", value=Decimal(100), unit="USD", period={"kind":"instant","end":"2023-12-31"},
        basis={"scope":"consolidated","currency":"USD","accounting_basis":"US_GAAP"}, available_at=CAPTURE,
        captured_at=CAPTURE, materialized_at=CAPTURE)
    db_session.add(canonical); db_session.flush()
    for domain, identity in (("source_financial_fact", source.id), ("fundamental_fact", canonical.id)):
        db_session.add(SelectionDecision(record_domain=domain, record_id=identity, status="accepted", rule_version="daily-test",
            evidence_grade="metadata_grade", reason="fixture", actor="test", recorded_at=CAPTURE))
    db_session.flush()
    resolution = derive_daily_publication(document, filing, market="US", metadata_records=[
        {"accessionNumber": filing.accession, "filingDate": "2024-02-02"}])
    append_daily_publication_evidence(db_session, document=document, filing=filing, resolution=resolution,
        captured_at=CAPTURE, recorded_at=CAPTURE)
    before = materialize_metrics(db_session, as_of=datetime(2024,2,2,23,tzinfo=timezone.utc), query_version="release_query_v8_daily",
        materialized_at=CAPTURE, issuer_ids=[filing.issuer_id], metric_ids=[])
    after = materialize_metrics(db_session, as_of=datetime(2024,2,5,15,tzinfo=timezone.utc), query_version="release_query_v8_daily",
        materialized_at=CAPTURE, issuer_ids=[filing.issuer_id], metric_ids=[])
    assert before.canonical_inputs == 0
    assert after.canonical_inputs == 1


def test_daily_runner_multi_batch_commits_copy_cursor_before_orm_expiration(db_session, tmp_path):
    from backtest_data.catalog.models import Security
    from backtest_data.fundamentals.daily_publication_runner import backfill_daily_publications
    from backtest_data.fundamentals.domain import stable_hash
    from backtest_data.ingestion.models import TargetUniverseManifest
    document, filing = stored(db_session, {"filed":"2024-02-02"})
    other = FinancialSourceDocument(filing_id=filing.id, content_hash="b" * 64,
        source_url="https://www.sec.gov/metadata", available_at=CAPTURE, captured_at=CAPTURE,
        extraction_complete=True, inventory={}, parser_version="metadata_fixture", instance_key="second")
    security = Security(issuer_id=filing.issuer_id, market="US", source_identifier="daily-runner-test",
                        security_type="common_stock", name="Daily runner test")
    db_session.add_all([other,security]); db_session.flush()
    members = [{"market":"US","security_id":security.id,"issuer_id":filing.issuer_id}]
    target = TargetUniverseManifest(content_hash=stable_hash(members), scope={}, members=members, gaps=[], captured_at=CAPTURE)
    db_session.add(target); db_session.flush()
    manifest_id = target.id
    # Real PostgreSQL and default expiring ORM commits, borrowed inside the
    # fixture's outer rollback transaction so tests never persist their rows.
    connection = db_session.connection()
    class BorrowedConnection:
        def execution_options(self, **options):
            return self
        def __enter__(self):
            return connection
        def __exit__(self, *args):
            return False
    class FixtureDatabase:
        def connect(self):
            return BorrowedConnection()
    result = backfill_daily_publications(FixtureDatabase(), manifest_id=manifest_id,
        progress_path=tmp_path/"daily-progress.json", watermark=CAPTURE, batch_size=1, budget_seconds=30)
    assert result["status"] == "source_pass_complete"
    assert result["total_documents"] == result["documents_with_outcomes"] == 2
    assert result["qualified_documents"] == 2
