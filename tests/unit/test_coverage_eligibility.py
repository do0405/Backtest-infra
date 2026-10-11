"""Conservative two-axis eligibility, independent of quote quality."""
from datetime import date, datetime, timedelta, timezone

import pytest

from backtest_data.coverage.eligibility import evaluate_eligibility, normalize_calendar_rows
from backtest_data.coverage.eligibility_service import import_calendar_evidence, materialize_eligibility_overlay

START, END = date(2026, 10, 1), date(2026, 10, 7)
CUTOFF = datetime(2026, 10, 10, tzinfo=timezone.utc)


def calendar(*, complete=False, authoritative=False, rows=None, captured_at=CUTOFF):
    return dict(id="cal", assertion_hash="a" * 64, market="US", start=START, end=END,
        rows=rows or [{"date": "2026-10-01", "status": "open"}],
        complete_interval=complete, authoritative=authoritative, captured_at=captured_at,
        recorded_at=captured_at, status="accepted")


def listing(**kwargs):
    return dict(id="list", assertion_hash="b" * 64, security_id="security",
        listing_start=START, listing_end=None, scope_start=START, scope_end=END,
        complete_history=True, initial_listing_declared=False, lifecycle_ref="one-life",
        source="official_fixture", source_refs={"lifecycle_declaration_ref": "single-original-declaration"},
        identity_status="verified_security", grade="verified", captured_at=CUTOFF,
        recorded_at=CUTOFF, status="accepted", **kwargs)


def evaluate(calendars=(), listings=(), **kwargs):
    return evaluate_eligibility(start=START, end=END, security_id="security", market="US",
        calendars=calendars, listings=listings, evidence_cutoff=CUTOFF, **kwargs)


def test_open_rows_without_complete_interval_leave_missing_day_unknown():
    result = evaluate([calendar()], [listing()], profile="qualified")
    assert result["counts"]["unknown"] == 5  # broker open is not authoritative
    assert all(span["calendar"] == "unknown" for span in result["spans"])


def test_complete_provider_calendar_reports_closed_but_does_not_verify():
    result = evaluate([calendar(complete=True)], [listing()], profile="provider_declared_research")
    assert result["counts"]["reported_eligible_missing"] == 1
    assert result["counts"]["reported_closed"] == 4
    assert result["verified_expected_count"] == 0
    assert result["reported_expected_count"] == 1


def test_listing_start_is_inclusive_and_end_exclusive():
    evidence = listing()
    evidence.update(listing_start=date(2026, 10, 2), listing_end=date(2026, 10, 6))
    rows = [{"date": day, "status": "open"} for day in
        ("2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07")]
    result = evaluate([calendar(complete=True, authoritative=True, rows=rows)], [evidence], profile="qualified")
    assert result["counts"]["verified_not_listed"] == 3
    assert result["counts"]["verified_eligible_missing"] == 2


def test_relisting_intervals_union_without_ticker_reuse_certification():
    first, second = listing(), listing()
    first.update(listing_end=date(2026, 10, 2))
    second.update(id="list2", assertion_hash="c" * 64, listing_start=date(2026, 10, 6))
    rows = [{"date": day, "status": "open"} for day in
        ("2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07")]
    result = evaluate([calendar(authoritative=True, complete=True, rows=rows)], [first, second], profile="qualified")
    assert result["counts"]["verified_eligible_missing"] == 3
    assert result["counts"]["verified_not_listed"] == 2
    second.update(identity_status="unresolved_symbol")
    first.update(identity_status="unresolved_symbol")
    result = evaluate([calendar(authoritative=True, complete=True, rows=rows)], [first, second], profile="qualified")
    assert result["counts"]["unknown"] == 5


def test_late_evidence_cannot_change_an_earlier_cutoff():
    result = evaluate([calendar(complete=True, authoritative=True,
        captured_at=CUTOFF + timedelta(seconds=1))], [listing()], profile="qualified")
    assert result["counts"]["unknown"] == 5


def test_quotes_on_closed_or_prelisting_day_are_conflicts_not_exclusions():
    result = evaluate([calendar(complete=True, authoritative=True)], [listing()],
        profile="qualified", observed={date(2026, 10, 2)})
    assert result["counts"]["conflict"] == 1
    assert result["counts"]["verified_closed"] == 3


def test_quote_before_declared_listing_conflicts_on_listing_axis():
    evidence = listing()
    evidence.update(listing_start=date(2026, 10, 2))
    result = evaluate([], [evidence], profile="qualified", observed={START})
    assert result["counts"]["conflict"] == 1
    assert result["spans"][0]["listing"] == "verified_not_listed"


def test_independent_listing_histories_with_disagreeing_boundaries_conflict():
    first, second = listing(), listing()
    second.update(id="alternative", assertion_hash="f" * 64,
        lifecycle_ref="another-life", listing_start=date(2026, 10, 2))
    result = evaluate([], [first, second], profile="qualified")
    assert result["counts"]["conflict"] == 1


def test_same_lifecycle_cross_capture_contradiction_cannot_hide_in_interval_union():
    first, second = listing(), listing()
    second.update(id="later", assertion_hash="f" * 64, listing_start=date(2026, 10, 2),
        captured_at=CUTOFF - timedelta(seconds=1), recorded_at=CUTOFF - timedelta(seconds=1))
    result = evaluate([], [first, second], profile="qualified")
    assert result["counts"]["conflict"] == 1


def test_same_lifecycle_same_clock_distinct_declarations_still_conflict():
    first, second = listing(), listing()
    second.update(id="alternative", assertion_hash="f" * 64, listing_start=date(2026, 10, 2),
        source_refs={"lifecycle_declaration_ref": "another-original-declaration"})
    result = evaluate([], [first, second], profile="qualified")
    assert result["counts"]["conflict"] == 1


def test_provider_listing_scope_ends_at_its_snapshot_without_forward_projection():
    evidence = listing()
    evidence.update(identity_status="source_declared_security", grade="source_declared",
        complete_history=False, initial_listing_declared=True, scope_start=START,
        scope_end=date(2026, 10, 2), listing_start=date(2026, 10, 2))
    rows = [{"date": day, "status": "open"} for day in
        ("2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07")]
    result = evaluate([calendar(complete=True, rows=rows)], [evidence], profile="provider_declared_research")
    assert result["counts"]["reported_not_listed"] == 1
    assert result["counts"]["reported_eligible_missing"] == 1
    assert result["counts"]["unknown"] == 3


def test_provider_delisting_effective_declaration_is_labeled_after_boundary():
    evidence = listing()
    evidence.update(identity_status="source_declared_security", grade="source_declared",
        complete_history=False, listing_end=date(2026, 10, 6))
    result = evaluate([], [evidence], profile="provider_declared_research")
    assert result["counts"]["reported_not_listed"] == 2


def test_conflicting_calendar_assertions_remain_conflict():
    first = calendar(complete=True, authoritative=True)
    second = calendar(authoritative=True, rows=[{"date": "2026-10-02", "status": "open"}])
    second.update(id="other", assertion_hash="d" * 64)
    result = evaluate([first, second], [listing()], profile="qualified")
    assert result["counts"]["conflict"] == 1


def test_all_disjoint_spans_conserve_provisional_positions_and_clean_counts():
    clean = {date(2026, 10, 1), date(2026, 10, 6)}
    result = evaluate([], [], clean=clean, observed=clean, profile="qualified")
    positions = [day for span in result["spans"] for day in span["dates"]]
    assert len(positions) == len(set(positions)) == 5
    assert sum(result["counts"].values()) == 5
    assert sum(span["clean_count"] for span in result["spans"]) == 2
    assert result["baseline_clean_count"] == 2
    assert sum(result["calendar_counts"].values()) == 5
    assert sum(result["listing_counts"].values()) == 5


def test_rejected_snapshot_and_form25_filing_do_not_qualify_listing():
    evidence = listing()
    evidence.update(status="rejected", identity_status="unresolved_symbol", grade="source_declared")
    result = evaluate([calendar(complete=True, authoritative=True)], [evidence], profile="qualified")
    assert result["counts"]["verified_closed"] == 4
    assert result["counts"]["unknown"] == 1


@pytest.mark.parametrize("rows", [[], [{"date": "2026-10-02"}, {"date": "2026-10-02"}],
    [{"date": "2027-01-01"}], [{"date": "2026-10-01", "status": "holiday_guess"}]])
def test_complete_calendar_rejects_empty_duplicate_outside_or_untyped_rows(rows):
    with pytest.raises(ValueError):
        normalize_calendar_rows(rows, start=START, end=END, complete_interval=True)


def test_broker_calendar_cannot_be_labeled_authoritative_without_explicit_official_proof():
    with pytest.raises(ValueError, match="authoritative"):
        import_calendar_evidence(None, market="US", source="alpaca", calendar_version="v1",
            returned_rows=[{"date": "2026-10-01"}], start=START, end=END, captured_at=CUTOFF,
            source_refs={"body_hash": "a" * 64, "capture_id": "fixture"},
            complete_interval=True, authoritative=True)


@pytest.mark.parametrize("limit", [0, 101, True])
def test_overlay_rejects_invalid_page_limit_before_touching_database(limit):
    with pytest.raises(ValueError, match="limit"):
        materialize_eligibility_overlay(None, "run", limit=limit, evidence_cutoff=CUTOFF)
