"""Exercise real PostgreSQL persistence against the dedicated _test database."""
from datetime import date, datetime, timedelta, timezone
import importlib

import pytest
from sqlalchemy import func, select, text

from backtest_data.catalog.models import Issuer, Security, SourceContract
from backtest_data.db import new_id, utcnow
from backtest_data.fundamentals.models import Filing, SourceFinancialFact
from backtest_data.fundamentals.public_availability import append_publication_evidence, numeric_binding
from backtest_data.fundamentals.storage import store_extraction
from backtest_data.fundamentals.xbrl import parse_xbrl
from backtest_data.ingestion.models import CollectionJob, RawObject, TargetUniverseManifest
from backtest_data.prices.models import PriceObservation, VWAPDailyStat
from backtest_data.releases.models import SelectionDecision


pytestmark = pytest.mark.integration
CAPTURE = datetime(2026, 10, 8, tzinfo=timezone.utc)
START, END = date(2026, 10, 1), date(2026, 10, 7)


def coverage():
    return importlib.import_module("backtest_data.coverage.service")


def target(session, kinds=("common_stock", "etf", "etn"), missing=True):
    members = []
    for index, kind in enumerate(kinds):
        issuer = Issuer(market="US", source_identifier=new_id(), name="Coverage issuer")
        session.add(issuer)
        session.flush()
        security = Security(market="US", source_identifier=new_id(), security_type=kind,
            name="Coverage target", issuer_id=issuer.id)
        session.add(security)
        session.flush()
        members.append({"market": "US", "security_id": security.id, "identifier": security.source_identifier,
            "symbol": "TEST" + str(index), "security_type": kind})
    if missing:
        members.append({"market": "KR", "security_id": new_id(), "identifier": "missing", "security_type": "unclassified"})
    manifest = TargetUniverseManifest(content_hash=(new_id().replace("-", "") * 2),
        scope={"output_start": str(START), "output_end": str(END)}, members=members, gaps=[])
    session.add(manifest)
    session.flush()
    return manifest


def run(session, manifest, **kwargs):
    return coverage().start_coverage_run(session, manifest.id, output_start=START, output_end=END,
        watermark=utcnow() + timedelta(seconds=5), **kwargs)


def finish(session, identity, batch_size=2):
    result = coverage().advance_coverage_run(session, identity, batch_size=batch_size)
    while result["status"] != "audit_complete":
        result = coverage().advance_coverage_run(session, identity, batch_size=batch_size)
    return coverage().coverage_report(session, identity)


def summaries(session, identity):
    models = importlib.import_module("backtest_data.coverage.models")
    return list(session.scalars(select(models.CoverageSummary).where(models.CoverageSummary.run_id == identity)))


def add_price(session, security_id, day, *, basis="as_traded", decision="accepted", capture=CAPTURE,
        evidence=None, volume=100, rule="raw_price_structure_v1", grade="contract_verified"):
    contract = SourceContract(source="coverage_test", version=new_id(), domain="prices", market="US",
        specification={}, qualification="pending", content_hash="a" * 64)
    raw = RawObject(content_hash=new_id().replace("-", "") * 2, storage_key=new_id(), byte_count=1)
    session.add_all([contract, raw])
    session.flush()
    row = PriceObservation(security_id=security_id, source_contract_id=contract.id, raw_id=raw.id,
        session=day, basis=basis, feed="sip", venue="US", trade_scope="session_total", currency="USD",
        quantity_unit="shares", open=10, high=12, low=9, close=11, volume=volume, notional=None,
        signature=new_id().replace("-", "") * 2, available_at=capture, captured_at=capture, evidence=evidence or {})
    session.add(row)
    session.flush()
    session.add(SelectionDecision(record_domain="price_observation", record_id=row.id, status=decision,
        rule_version=rule, evidence_grade=grade, reason="test structure",
        actor="coverage_test", recorded_at=CAPTURE))
    session.flush()
    return row


def test_every_manifest_member_is_sql_persisted_and_resumption_is_exact(db_session):
    manifest = target(db_session)
    identity = run(db_session, manifest)
    report = coverage().advance_coverage_run(db_session, identity, batch_size=2)
    assert report["target_count"] == 4 and report["processed_count"] == 2
    assert report["coverage_complete"] is False
    report = finish(db_session, identity)
    assert report["status"] == "audit_complete" and report["processed_count"] == 4
    assert report["market_counts"] == {"KR": 1, "US": 3}
    assert report["expected_market_counts"] == {"KR": 4507, "US": 13251}
    assert report["denominator_matches_expected"] is False
    assert report["gap_counts"]["missing_security"] == 1
    assert len(summaries(db_session, identity)) == 12
    repeated = coverage().advance_coverage_run(db_session, identity, batch_size=2)
    assert repeated["processed_count"] == 4 and len(summaries(db_session, identity)) == 12


def test_terminated_traversal_cannot_certify_missing_price_dates(db_session):
    manifest = target(db_session, ("common_stock",), missing=False)
    member = manifest.members[0]
    db_session.add(CollectionJob(identity_hash=new_id().replace("-", "") * 2, source="alpaca",
        domain="prices", entity_id=member["security_id"], request={"manifest_id": manifest.id}, status="succeeded",
        checkpoint={"traversal_complete": True, "history_coverage_qualified": False}))
    db_session.flush()
    report = finish(db_session, run(db_session, manifest))
    price = next(item for item in summaries(db_session, report["run_id"]) if item.domain == "prices")
    assert price.clean_count == 0 and price.missing_count == 5
    assert price.details["collector"]["traversal_complete_jobs"] == 1
    assert report["coverage_complete"] is False
    assert report["gap_counts"]["provisional_missing_date"] >= 5
    assert "historical_eligibility_unknown" in report["gap_counts"]


def test_raw_clean_price_dates_ignore_acquisition_pit_but_exclude_future_and_adjusted(db_session):
    manifest = target(db_session, ("common_stock",), missing=False)
    sid = manifest.members[0]["security_id"]
    add_price(db_session, sid, START)
    add_price(db_session, sid, START)  # Distinct captures do not inflate date counts.
    add_price(db_session, sid, date(2026, 10, 2), basis="adjusted")
    add_price(db_session, sid, date(2026, 10, 8))
    rejected = add_price(db_session, sid, date(2026, 10, 5))
    db_session.add(SelectionDecision(record_domain="price_observation", record_id=rejected.id, status="rejected",
        rule_version="correction", evidence_grade="invalid", reason="later rejection", actor="test",
        recorded_at=CAPTURE + timedelta(seconds=1)))
    db_session.flush()
    identity = run(db_session, manifest)
    finish(db_session, identity)
    price = next(item for item in summaries(db_session, identity) if item.domain == "prices")
    assert price.observed_count == 3 and price.clean_count == 1
    assert price.missing_count == 4
    assert price.details["availability_policy"] == "session_bounded_no_acquisition_pit"
    assert price.details["future_actions_applied"] is False


def test_unsupported_sources_and_fundamental_not_applicable_keep_target_members(db_session):
    manifest = target(db_session, ("etf", "etn", "etp_unknown"), missing=False)
    for member in manifest.members:
        db_session.add(CollectionJob(identity_hash=new_id().replace("-", "") * 2, source="sec",
            domain="fundamentals", entity_id=member["security_id"], request={}, status="unsupported",
            checkpoint={"reason": "source_not_supported"}))
    db_session.flush()
    identity = run(db_session, manifest)
    report = finish(db_session, identity)
    funds = [item for item in summaries(db_session, identity) if item.domain == "fundamentals"]
    assert sum(item.details["applicability"] == "not_applicable" for item in funds) == 2
    assert all(item.expected_count is None for item in funds)
    assert report["target_count"] == 3
    assert report["gap_counts"]["source_unsupported"] >= 1
    assert report["gap_counts"]["financial_period_expectation_unknown"] == 1


def financial_document(session, manifest, *, accession=None, amount=b"100"):
    security = session.get(Security, manifest.members[0]["security_id"])
    xml = b'''<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:c="urn:custom" xmlns:i="http://www.xbrl.org/2003/iso4217">
    <context id="ctx"><entity><identifier scheme="http://www.sec.gov/CIK">1</identifier></entity><period><instant>2023-12-31</instant></period></context>
    <unit id="USD"><measure>i:USD</measure></unit><c:Amount contextRef="ctx" unitRef="USD" decimals="0">100</c:Amount></xbrl>'''
    xml = xml.replace(b">100</c:Amount>", b">" + amount + b"</c:Amount>")
    document = store_extraction(session, issuer_id=security.issuer_id, source="sec", accession=accession or new_id(), form="10-K",
        source_url="https://www.sec.gov/original.xml", content=xml, result=parse_xbrl(xml),
        available_at=CAPTURE, captured_at=CAPTURE)
    filing = session.get(Filing, document.filing_id)
    facts = list(session.scalars(select(SourceFinancialFact).where(SourceFinancialFact.document_id == document.id)))
    binding = numeric_binding(document, facts)
    evidence = {"binding": {"document_id": document.id, "filing_id": filing.id, "issuer_id": filing.issuer_id,
        "source": filing.source, "accession": filing.accession, "document_hash": document.content_hash},
        "proof": {"kind": "sec_dated_feed_original", "public_day": "2024-03-01", "issuer_id": filing.issuer_id,
            "accession": filing.accession, "source_url": "https://www.sec.gov/Archives/edgar/Feed/2024/QTR1/20240301.nc.tar.gz",
            "source_body_hash": "b" * 64, "original_body_hash": document.content_hash,
            "membership_verified": True, "original_version_verified": True, "member_hash": document.content_hash,
            "member_name": "original.xml", "archive_url": "https://www.sec.gov/Archives/edgar/Feed/2024/QTR1/20240301.nc.tar.gz"},
        "numeric_binding": {**binding, "original_digest": binding["sql_digest"]}}
    return document, filing, evidence


def test_actual_financial_periods_require_exact_original_public_certificate(db_session):
    manifest = target(db_session, ("common_stock",), missing=False)
    document, filing, evidence = financial_document(db_session, manifest)
    identity = coverage().start_coverage_run(db_session, manifest.id, watermark=utcnow() + timedelta(seconds=5))
    report = finish(db_session, identity)
    financial = next(item for item in summaries(db_session, identity) if item.domain == "fundamentals")
    assert financial.observed_count == 1 and financial.clean_count == 0
    assert report["gap_counts"]["uncertified_public_pit_period"] == 1
    models = importlib.import_module("backtest_data.coverage.models")
    period = db_session.scalar(select(models.CoverageFinancialPeriod).where(models.CoverageFinancialPeriod.run_id == identity))
    assert period.period_end == date(2023, 12, 31) or period.instant == date(2023, 12, 31)
    assert period.status == "uncertified_public_pit_period" and period.qualified_source_fact_count == 0
    certificate = append_publication_evidence(db_session, document=document, filing=filing, status="qualified",
        evidence=evidence, public_day=date(2024, 3, 1), captured_at=CAPTURE, recorded_at=utcnow())
    second = coverage().start_coverage_run(db_session, manifest.id, watermark=utcnow() + timedelta(seconds=5))
    finish(db_session, second)
    financial = next(item for item in summaries(db_session, second) if item.domain == "fundamentals")
    assert financial.clean_count == 1
    assert financial.details["qualified_source_fact_count"] == 1
    append_publication_evidence(db_session, document=document, filing=filing, status="rejected",
        evidence={"reason": "wrong_original"}, captured_at=CAPTURE, recorded_at=utcnow(),
        supersedes_assertion_hash=certificate.assertion_hash)
    third = coverage().start_coverage_run(db_session, manifest.id, watermark=utcnow() + timedelta(seconds=5))
    finish(db_session, third)
    assert next(item for item in summaries(db_session, third) if item.domain == "fundamentals").clean_count == 0


def test_batch_rolls_back_without_advancing_cursor_and_invalid_size_is_rejected(db_session):
    manifest = target(db_session, ("common_stock",), missing=False)
    identity = run(db_session, manifest)
    with pytest.raises(ValueError, match="batch"):
        coverage().advance_coverage_run(db_session, identity, batch_size=0)
    with pytest.raises(RuntimeError, match="rollback"):
        with db_session.begin_nested():
            coverage().advance_coverage_run(db_session, identity, batch_size=1)
            raise RuntimeError("rollback")
    assert coverage().coverage_report(db_session, identity)["processed_count"] == 0
    assert summaries(db_session, identity) == []
    assert finish(db_session, identity)["processed_count"] == 1


def test_invalid_engine_batch_does_not_leave_an_orphan_run(db_session):
    manifest = target(db_session, ("etf",), missing=False)
    models = importlib.import_module("backtest_data.coverage.models")
    before = db_session.scalar(select(func.count()).select_from(models.CoverageRun))
    with pytest.raises(ValueError, match="batch"):
        coverage().materialize_coverage(db_session.connection(), manifest_id=manifest.id, batch_size=0)
    assert db_session.scalar(select(func.count()).select_from(models.CoverageRun)) == before


def test_entire_17758_target_is_snapshotted_without_sample_or_unsupported_reduction(db_session):
    members = [{"security_id": new_id(), "market": "KR" if index < 4507 else "US",
        "identifier": "unresolved:" + str(index), "security_type": "etf" if index % 2 else "etn"}
        for index in range(17758)]
    manifest = TargetUniverseManifest(content_hash=new_id().replace("-", "") * 2, scope={}, members=members, gaps=[])
    db_session.add(manifest)
    db_session.flush()
    identity = run(db_session, manifest)
    report = coverage().advance_coverage_run(db_session, identity, batch_size=100)
    assert report["target_count"] == 17758 and report["processed_count"] == 100
    assert report["market_counts"] == {"KR": 4507, "US": 13251}
    assert report["denominator_matches_expected"] is True
    assert len(summaries(db_session, identity)) == 300
    assert report["coverage_complete"] is False and report["status"] == "running"


def test_engine_wrapper_commits_batches_and_resumes_original_configuration(db_session):
    manifest = target(db_session, ("etf", "etn", "etp_unknown"), missing=False)
    result = coverage().materialize_coverage(db_session.connection(), manifest_id=manifest.id,
        batch_size=1, max_batches=1, output_start=START, output_end=END)
    assert result["processed_count"] == 1
    resumed = coverage().materialize_coverage(db_session.connection(), run_id=result["run_id"],
        batch_size=1, max_batches=10)
    assert resumed["processed_count"] == 3 and resumed["status"] == "audit_complete"
    with pytest.raises(ValueError, match="original"):
        coverage().materialize_coverage(db_session.connection(), run_id=result["run_id"], output_end=END)


@pytest.mark.parametrize("volume", [None, "NaN", "Infinity"])
def test_clean_price_and_volume_dates_require_finite_observed_volume(db_session, volume):
    manifest = target(db_session, ("etf",), missing=False)
    add_price(db_session, manifest.members[0]["security_id"], START, volume=volume)
    identity = run(db_session, manifest)
    finish(db_session, identity)
    price = next(item for item in summaries(db_session, identity) if item.domain == "prices")
    assert price.observed_count == 1 and price.clean_count == 0


def test_vwap_acceptance_also_requires_exact_method_and_matched_scopes(db_session):
    manifest = target(db_session, ("etf",), missing=False)
    sid = manifest.members[0]["security_id"]
    price = add_price(db_session, sid, START)
    for day, method, evidence in [(START, "bar_proxy", {}), (date(2026, 10, 2), "matched_daily_statistics", {}),
            (date(2026, 10, 5), "trade_aggregated", {"matched_scope_verified": True,
                "numerator_trade_scope": "eligible_regular_trades", "denominator_trade_scope": "eligible_regular_trades"})]:
        stat = VWAPDailyStat(security_id=sid, source_contract_id=price.source_contract_id, raw_id=price.raw_id,
            session=day, sum_price_volume=1000, eligible_volume=100, trade_scope="eligible_regular_trades",
            method=method, basis="as_traded", available_at=CAPTURE, captured_at=CAPTURE,
            materialized_at=CAPTURE, evidence=evidence)
        db_session.add(stat)
        db_session.flush()
        db_session.add(SelectionDecision(record_domain="vwap_daily_stat", record_id=stat.id, status="accepted",
            rule_version="test", evidence_grade="verified", reason="test", actor="test", recorded_at=CAPTURE))
    db_session.flush()
    identity = run(db_session, manifest)
    finish(db_session, identity)
    stat = next(item for item in summaries(db_session, identity) if item.domain == "vwap")
    assert stat.observed_count == 3 and stat.clean_count == 1


def test_exact_certified_correction_uses_its_own_public_clock(db_session):
    manifest = target(db_session, ("common_stock",), missing=False)
    _, filing, _ = financial_document(db_session, manifest)
    document, filing, evidence = financial_document(db_session, manifest, accession=filing.accession, amount=b"200")
    assert document.revision_kind == "replacement"
    proof = evidence["proof"]
    proof.update(public_day="2024-04-01", source_url="https://www.sec.gov/Archives/edgar/Feed/2024/QTR2/20240401.nc.tar.gz",
        archive_url="https://www.sec.gov/Archives/edgar/Feed/2024/QTR2/20240401.nc.tar.gz")
    append_publication_evidence(db_session, document=document, filing=filing, status="qualified", evidence=evidence,
        public_day=date(2024, 4, 1), captured_at=CAPTURE, recorded_at=utcnow())
    identity = coverage().start_coverage_run(db_session, manifest.id, watermark=utcnow() + timedelta(seconds=5))
    finish(db_session, identity)
    financial = next(item for item in summaries(db_session, identity) if item.domain == "fundamentals")
    assert financial.clean_count == 1 and financial.details["qualified_source_fact_count"] == 1
    assert financial.details["qualified_revision_counts"] == {"replacement": 1}


def test_supplier_zero_and_unknown_rule_cannot_certify_clean_prices_without_toast_reads(db_session):
    manifest = target(db_session, ("etf",), missing=False)
    sid = manifest.members[0]["security_id"]
    add_price(db_session, sid, START, rule="kis_supplier_zero_transaction_structure_v1",
        grade="supplier_reported_unverified")
    add_price(db_session, sid, date(2026, 10, 2), rule="unregistered_price_policy")
    identity = run(db_session, manifest)
    finish(db_session, identity)
    price = next(item for item in summaries(db_session, identity) if item.domain == "prices")
    assert price.observed_count == 2 and price.clean_count == 0


def test_supplemental_readiness_is_explicit_without_inventing_index_membership(db_session):
    manifest = target(db_session, ("etf", "etn", "common_stock"), missing=False)
    identity = run(db_session, manifest)
    report = finish(db_session, identity)
    assert report["gap_counts"]["corporate_actions_verified_coverage_unavailable"] == 3
    assert report["gap_counts"]["historical_listing_identity_unknown"] == 3
    assert report["gap_counts"]["etf_etn_product_facts_missing"] == 2
    assert "index_membership_missing" not in report["gap_counts"]
    assert report["supplemental_readiness"]["index_membership_expected_universe"] == "unknown"
    models = importlib.import_module("backtest_data.coverage.models")
    gaps = list(db_session.scalars(select(models.CoverageGap).where(models.CoverageGap.run_id == identity,
        models.CoverageGap.reason == "corporate_actions_verified_coverage_unavailable")))
    assert all(gap.details["observed_event_count"] == 0 for gap in gaps)
    assert len(summaries(db_session, identity)) == 9


def test_resuming_a_pre_supplemental_run_persists_the_global_unknown_index_expectation(db_session):
    manifest = target(db_session, ("etf",), missing=False)
    identity = run(db_session, manifest)
    models = importlib.import_module("backtest_data.coverage.models")
    value = db_session.get(models.CoverageRun, identity)
    value.details = {key: item for key, item in value.details.items() if key != "supplemental_readiness"}
    db_session.flush()
    finish(db_session, identity)
    db_session.expire(value)
    assert value.details["supplemental_readiness"]["index_membership_expected_universe"] == "unknown"
