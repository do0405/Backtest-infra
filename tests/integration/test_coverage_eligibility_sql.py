"""Isolated PostgreSQL evidence immutability, bounded overlay and replay."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text

from backtest_data.catalog.models import Issuer, Security
from backtest_data.coverage.models import CoverageMember, CoverageRun, CoverageSummary
from backtest_data.coverage.eligibility_models import CalendarEligibilityEvidence, ListingEligibilityEvidence, EligibilityOverlay, EligibilityOverlayPage
from backtest_data.coverage.eligibility_service import (import_calendar_evidence,
    import_listing_evidence, import_krx_listing_evidence,
    materialize_eligibility_overlay, eligibility_overlay_report)
from backtest_data.db import new_id
from backtest_data.ingestion.models import TargetUniverseManifest

pytestmark = pytest.mark.integration
START, END = date(2026, 10, 1), date(2026, 10, 7)
CAPTURE = datetime(2026, 10, 8, tzinfo=timezone.utc)
CUTOFF = CAPTURE + timedelta(days=2)


def baseline(session, count=2, market="US"):
    manifest = TargetUniverseManifest(content_hash=new_id().replace("-", "") * 2,
        scope={}, members=[{"market": "US"}] * count, gaps=[])
    session.add(manifest)
    session.flush()
    run = CoverageRun(manifest_id=manifest.id, manifest_hash=manifest.content_hash,
        policy_version="whole_target_coverage_v1", output_start=START, output_end=END,
        watermark=CAPTURE, target_count=count, processed_count=count, cursor=count,
        status="audit_complete", details={"calendar_basis": "provisional_weekdays"})
    session.add(run)
    session.flush()
    for ordinal in range(1, count + 1):
        issuer = Issuer(market=market, source_identifier=new_id(), name="Overlay fixture")
        session.add(issuer)
        session.flush()
        security = Security(market=market, source_identifier=new_id(), security_type="common_stock",
            name="Overlay fixture", issuer_id=issuer.id)
        session.add(security)
        session.flush()
        member = CoverageMember(id=new_id(), run_id=run.id, ordinal=ordinal,
            security_id=security.id, market=market, member={}, done=True)
        session.add(member)
        session.flush()
        for domain in ("prices", "vwap"):
            session.add(CoverageSummary(run_id=run.id, member_id=member.id, market=market,
                domain=domain, observed_count=0, clean_count=0, expected_count=5,
                missing_count=5, coverage_complete=False, details={"expected_basis": "provisional_weekdays",
                "missing_date_spans": [{"start": str(START), "end": str(END), "count": 5}]}))
    session.flush()
    return run


def import_calendar(session, **kwargs):
    return import_calendar_evidence(session, market="US", source="alpaca", calendar_version="calendar-v1",
        returned_rows=[{"date": "2026-10-01", "open": "09:30", "close": "16:00"}],
        start=START, end=END, captured_at=CAPTURE, recorded_at=CAPTURE,
        source_refs={"source_url": "https://paper-api.alpaca.markets/v2/calendar", "body_hash": "a" * 64,
            "capture_id": "fixture"}, complete_interval=True, **kwargs)


def test_report_continues_after_one_hundred_single_member_pages(db_session):
    run = baseline(db_session, count=101)
    overlay = materialize_eligibility_overlay(db_session, run_id=run.id, limit=1,
        evidence_cutoff=CUTOFF)
    cursor = overlay['next_cursor']
    for _ in range(100):
        page = materialize_eligibility_overlay(db_session, run_id=run.id, limit=1,
            cursor=cursor)
        cursor = page['next_cursor']
    report = eligibility_overlay_report(db_session, overlay['overlay_id'], limit=100)
    assert len(report['members']) == 100
    assert report['next_cursor'] is not None
    tail = eligibility_overlay_report(db_session, overlay['overlay_id'], limit=100,
        cursor=report['next_cursor'])
    assert [m['ordinal'] for m in tail['members']] == [101]
    assert tail['next_cursor'] is None


def test_calendar_import_replay_is_idempotent_and_rows_are_immutable(db_session):
    first, replay = import_calendar(db_session), import_calendar(db_session)
    assert first.id == replay.id
    assert db_session.scalar(select(func.count()).select_from(CalendarEligibilityEvidence)) == 1
    with pytest.raises(Exception, match="immutable"):
        with db_session.begin_nested():
            db_session.execute(text("UPDATE calendar_eligibility_evidence SET source='changed' WHERE id=:id"), {"id": first.id})


def test_overlay_pages_preserve_baseline_and_replay_without_new_rows(db_session):
    run = baseline(db_session)
    original = (run.cursor, run.processed_count, run.watermark, run.manifest_hash)
    import_calendar(db_session)
    first = materialize_eligibility_overlay(db_session, run.id, limit=1, evidence_cutoff=CUTOFF)
    assert len(first["members"]) == 1 and first["next_cursor"]
    repeated = materialize_eligibility_overlay(db_session, run.id, limit=1, evidence_cutoff=CUTOFF)
    assert repeated == first
    second = materialize_eligibility_overlay(db_session, run.id, limit=1, cursor=first["next_cursor"], evidence_cutoff=CUTOFF)
    assert len(second["members"]) == 1 and second["next_cursor"] is None
    assert db_session.scalar(select(func.count()).select_from(EligibilityOverlayPage)) == 2
    assert original == (run.cursor, run.processed_count, run.watermark, run.manifest_hash)
    report = eligibility_overlay_report(db_session, first["overlay_id"], limit=1)
    assert len(report["members"]) == 1
    assert report["baseline_target_count"] == 2
    assert report["baseline_watermark"] == CAPTURE.isoformat()
    assert report["evidence_cutoff"] == CUTOFF.isoformat()


def test_overlay_cursor_and_materialization_roll_back_together(db_session):
    run = baseline(db_session, 1)
    with pytest.raises(RuntimeError, match="rollback"):
        with db_session.begin_nested():
            materialize_eligibility_overlay(db_session, run.id, limit=1, evidence_cutoff=CUTOFF)
            raise RuntimeError("rollback")
    assert db_session.scalar(select(func.count()).select_from(EligibilityOverlay)) == 0
    assert db_session.scalar(select(func.count()).select_from(EligibilityOverlayPage)) == 0
    assert len(materialize_eligibility_overlay(db_session, run.id, limit=1, evidence_cutoff=CUTOFF)["members"]) == 1


def test_listing_security_binding_requires_evidence_and_rejects_invalid_snapshot(db_session):
    run = baseline(db_session, 1)
    member = db_session.scalar(select(CoverageMember).where(CoverageMember.run_id == run.id))
    rejected = import_listing_evidence(db_session, security_id=member.security_id, market="US", source="alpha_vantage",
        listing_start=START, listing_end=None, scope_start=START, scope_end=END,
        captured_at=CAPTURE, recorded_at=CAPTURE, identity_status="unresolved_symbol", grade="source_declared",
        source_refs={"observation_id": "fixture", "body_hash": "b" * 64},
        evidence={"snapshot_scope_valid": False}, complete_history=False, initial_listing_declared=True)
    assert rejected.status == "rejected"
    result = materialize_eligibility_overlay(db_session, run.id, limit=1, evidence_cutoff=CUTOFF)
    assert result["members"][0]["domains"]["prices"]["counts"]["unknown"] == 5


@pytest.mark.parametrize("limit", [0, 101, True])
def test_invalid_limit_never_creates_overlay(db_session, limit):
    run = baseline(db_session, 1)
    with pytest.raises(ValueError, match="limit"):
        materialize_eligibility_overlay(db_session, run.id, limit=limit, evidence_cutoff=CUTOFF)
    assert db_session.scalar(select(func.count()).select_from(EligibilityOverlay)) == 0


def test_later_recorded_evidence_cannot_enter_an_existing_cutoff(db_session):
    run = baseline(db_session, 1)
    first = materialize_eligibility_overlay(db_session, run.id, limit=1, evidence_cutoff=CUTOFF)
    import_calendar_evidence(db_session, market="US", source="alpaca", calendar_version="later",
        returned_rows=[{"date": "2026-10-01"}], start=START, end=END,
        captured_at=CUTOFF + timedelta(seconds=1), recorded_at=CUTOFF + timedelta(seconds=1),
        source_refs={"body_hash": "c" * 64, "capture_id": "later"}, complete_interval=True)
    assert materialize_eligibility_overlay(db_session, run.id, limit=1, evidence_cutoff=CUTOFF) == first


def test_krx_listing_annotation_preserves_raw_refs_and_nonidentity_issues(db_session, tmp_path):
    import json
    from backtest_data.ingestion.http import ProviderResponse
    from backtest_data.prices.krx_quote_storage import store_krx_quotes
    run = baseline(db_session, 1, market="KR")
    member = db_session.scalar(select(CoverageMember).where(CoverageMember.run_id == run.id))
    payload = {"OutBlock_1": [{"ISU_CD": "KR7095570009", "ISU_SRT_CD": "095570", "ISU_NM": "Fixture",
        "ISU_ENG_NM": "Fixture", "LIST_DD": "20261002", "PARVAL": "", "LIST_SHRS": "100"}]}
    response = ProviderResponse(json.dumps(payload).encode(), 200, CAPTURE, "application/json")
    stored = store_krx_quotes(db_session, "kospi_basic", END, response, payload, tmp_path)
    row = import_krx_listing_evidence(db_session, observation_id=stored["inserted_ids"][0],
        security_id=member.security_id, source_isin="KR7095570009", recorded_at=CAPTURE)
    assert row.status == "accepted" and row.grade == "source_declared"
    assert row.identity_status == "source_declared_security"
    assert row.source_refs["raw_id"] and row.source_refs["capture_id"]
    assert row.evidence["issues"] and not row.evidence["historical_identity_verified"]
    result = materialize_eligibility_overlay(db_session, run.id, limit=1, evidence_cutoff=CUTOFF)
    assert result["members"][0]["domains"]["prices"]["counts"]["reported_not_listed"] == 1
    qualified = materialize_eligibility_overlay(db_session, run.id, limit=1, evidence_cutoff=CUTOFF, profile="qualified")
    assert qualified["members"][0]["domains"]["prices"]["counts"]["unknown"] == 5


def test_original_clean_quotes_outside_provisional_grid_are_preserved(db_session):
    run = baseline(db_session, 1)
    summary = db_session.scalar(select(CoverageSummary).where(CoverageSummary.run_id == run.id,
        CoverageSummary.domain == "prices"))
    # Original coverage counts include observed weekend quotes, while missing
    # spans describe only the provisional weekday grid.
    summary.clean_count = 1
    summary.observed_count = 1
    db_session.flush()
    result = materialize_eligibility_overlay(db_session, run.id, limit=1, evidence_cutoff=CUTOFF)
    domain = result["members"][0]["domains"]["prices"]
    assert domain["baseline_stored_clean_count"] == 1
    assert domain["baseline_clean_count"] == 0
    assert domain["baseline_clean_outside_grid_count"] == 1
    assert sum(domain["counts"].values()) == 5
