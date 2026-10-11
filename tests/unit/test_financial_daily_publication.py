from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal as D
import importlib
from pathlib import Path
from types import SimpleNamespace as N

import pytest

from backtest_data.fundamentals.domain import FinancialFact
from backtest_data.fundamentals.selection import AmbiguousFinancialRevision


CAPTURE = datetime(2026, 10, 10, tzinfo=timezone.utc)
OLD = "0000000001-24-000001"
CORRECTION = "0000000001-24-000002"


def api():
    try:
        return importlib.import_module("backtest_data.fundamentals.daily_publication")
    except ModuleNotFoundError:
        pytest.fail("Daily metadata publication policy is not implemented")


def filing(source="sec", accession=OLD):
    return N(id="filing1", issuer_id="issuer1", source=source, accession=accession,
             form="10-K", captured_at=CAPTURE, evidence={}, published_at=None)


def document(**changes):
    return N(**(dict(id="doc1", filing_id="filing1", revision_kind="original", captured_at=CAPTURE,
                   extraction_complete=True, inventory={}, parser_version="xbrl_v1") | changes))


def source(receipt, **changes):
    return N(**(dict(id="source1", document_id="doc1", issuer_id="issuer1", context_id="ctx1", captured_at=CAPTURE,
        evidence={"source_row": {"rcept_no": receipt}}, dimensions={}, value=D("100"), unit="KRW",
        period_kind="instant", instant=date(2023, 12, 31), period_start=None, period_end=date(2023, 12, 31), scope="consolidated") | changes))


def derive(day="2024-02-02", accession=OLD, **kwargs):
    return api().derive_daily_publication(document(), filing(accession=accession), market="US",
        metadata_records=[{"accessionNumber": accession, "filingDate": day}], **kwargs)


def fact(value, resolution, identity):
    return FinancialFact(identity, "assets_total", D(value), "instant", None, date(2023, 12, 31),
        "USD", "consolidated", resolution["effective_available_at"], CAPTURE,
        currency="USD", accounting_basis="US_GAAP", evidence={"public_availability": resolution})


def test_correction_is_visible_only_after_its_own_publication_day():
    original = fact("100", derive(), "old")
    correction = fact("80", derive("2024-04-05", CORRECTION), "corrected")
    selected = api().select_daily_facts_asof([original, correction], as_of=datetime(2024, 4, 5, 23, tzinfo=timezone.utc))
    assert [x.value for x in selected] == [D("100")]
    selected = api().select_daily_facts_asof([original, correction], as_of=datetime(2024, 4, 8, 14, tzinfo=timezone.utc))
    assert [x.value for x in selected] == [D("80")]


def test_distinct_public_days_can_share_first_market_session_and_correct_in_public_order():
    original = fact("100", derive("2024-02-02"), "old")
    amended = fact("80", derive("2024-02-03", CORRECTION), "new")
    assert original.available_at == amended.available_at
    selected = api().select_daily_facts_asof([original, amended], as_of=CAPTURE)
    assert [x.value for x in selected] == [D("80")]


def test_source_precision_public_day_timestamp_never_moves_to_previous_us_day():
    f = filing()
    f.published_at = datetime(2024,2,2,tzinfo=timezone.utc)
    f.evidence = {"publication_precision":"public_day"}
    result = api().derive_daily_publication(document(), f, market="US")
    assert result["public_day"] == date(2024,2,2)


def test_filing_own_explicit_day_metadata_is_bound_to_its_exact_version():
    f = filing()
    f.evidence = {"filed":"2024-02-02"}
    result = api().derive_daily_publication(document(), f, market="US")
    assert result["status"] == "qualified"
    assert result["public_day"] == date(2024,2,2)


def test_daily_dart_json_accepts_returned_receipt_without_original_archive():
    returned = "20240405000001"
    f = filing("opendart", "20240202000001")
    result = api().derive_daily_publication(document(parser_version="opendart_financial_json_v3"), f, market="KR",
        source_fact=source(returned), metadata_records=[{"rcept_no": returned, "rcept_dt": "20240405"}])
    assert result["status"] == "qualified"
    assert result["source_version_ref"] == returned
    assert result["public_day"] == date(2024, 4, 5)
    assert result["effective_available_at"] == datetime(2024, 4, 8, 0, tzinfo=timezone.utc)
    assert result["evidence_grade"] == "metadata_grade"
    assert result["original_archive_verified"] is False


def test_returned_dart_receipt_date_fallback_never_uses_requested_old_receipt():
    result = api().derive_daily_publication(document(), filing("opendart", "20240202000001"), market="KR",
        source_fact=source("20240405000001"))
    assert result["public_day"] == date(2024, 4, 5)
    assert result["metadata_date_basis"] == "receipt_date_encoded_fallback"


def test_accounting_period_is_never_used_as_publication_metadata():
    result = api().derive_daily_publication(document(), filing(), market="US",
        metadata_records=[{"accessionNumber": OLD, "reportDate": "2023-12-31", "period_end": "2023-12-31"}])
    assert result["status"] == "unavailable"
    assert result["public_day"] is None
    assert result["effective_available_at"] is None


def test_sec_source_accn_and_filed_metadata_override_requested_accession():
    row = source("unused", evidence={"source_row": {"accn": CORRECTION, "filed": "2024-04-05"}})
    result = api().derive_daily_publication(document(), filing(), market="US", source_fact=row)
    assert result["source_version_ref"] == CORRECTION
    assert result["public_day"] == date(2024, 4, 5)


def test_sec_accn_and_filed_metadata_can_be_direct_source_evidence():
    row = source("unused", evidence={"accn": CORRECTION, "filed": "2024-04-05"})
    result = api().derive_daily_publication(document(), filing(), market="US", source_fact=row)
    assert result["source_version_ref"] == CORRECTION
    assert result["status"] == "qualified"
    assert result["public_day"] == date(2024, 4, 5)


def test_known_exchange_calendar_is_preferred_to_weekday_fallback():
    session = N(id="session1", market="US", session=date(2024, 7, 5), calendar_version="known",
                open_at=datetime(2024, 7, 5, 13, 30, tzinfo=timezone.utc))
    result = derive("2024-07-03", sessions=[session])
    assert result["effective_available_at"] == session.open_at
    assert result["calendar_basis"] == "exchange_session:known"
    assert result["calendar_evidence_ids"] == ["session1"]


def test_weekday_fallback_is_explicit_and_uses_issuer_market_timezone():
    result = derive()
    assert result["effective_available_at"] == datetime(2024, 2, 5, 14, 30, tzinfo=timezone.utc)
    assert result["calendar_basis"] == "next_weekday_market_open_fallback"


def test_conflicting_metadata_dates_do_not_choose_arbitrary_publication():
    result = api().derive_daily_publication(document(), filing(), market="US", metadata_records=[
        {"accessionNumber": OLD, "filingDate": "2024-02-02"},
        {"accessionNumber": OLD, "filingDate": "2024-02-05"}])
    assert result["status"] == "unavailable"
    assert result["reason"] == "ambiguous_publication_metadata"


def test_replaced_same_accession_is_not_backdated_to_original_metadata_day():
    result = api().derive_daily_publication(document(revision_kind="replacement", inventory={"correction_time_unknown": True}),
        filing(), market="US", metadata_records=[{"accessionNumber": OLD, "filingDate": "2024-02-02"}])
    assert result["status"] == "unavailable"
    assert result["reason"] == "same_version_correction_day_unknown"


def test_same_context_conflicting_numbers_on_same_public_day_fail_closed():
    first, second = fact("100", derive(), "a"), fact("80", derive(), "b")
    with pytest.raises(AmbiguousFinancialRevision):
        api().select_daily_facts_asof([first, second], as_of=CAPTURE)


def test_incompatible_contexts_remain_separate_and_observed_mode_preserves_capture_gate():
    first = fact("100", derive(), "a")
    other = replace(first, id="b", dimensions=(("segment", "other"),), value=D("80"))
    assert len(api().select_daily_facts_asof([first, other], as_of=CAPTURE)) == 2
    assert not api().select_daily_facts_asof([first], as_of=datetime(2024, 4, 1, tzinfo=timezone.utc), history_mode="observed_system")


def test_daily_migration_is_frozen_append_only_and_independent_from_strict_table():
    path = Path("alembic/versions/c71302d6b909_daily_publication.py")
    assert path.exists(), "daily policy additive migration is missing"
    text = path.read_text()
    assert "c71302d6b908" in text
    assert "financial_daily_publication_evidence" in text
    assert "backtest_immutable" in text
    assert "from backtest_data" not in text


def test_append_rejects_source_issuer_mismatch_before_any_sql():
    returned = "20240405000001"
    f = filing("opendart", returned)
    row = source(returned, issuer_id="wrong-issuer")
    resolution = api().derive_daily_publication(document(), f, market="KR")
    with pytest.raises(ValueError, match="binding"):
        api().append_daily_publication_evidence(None, document=document(), filing=f, source_fact=row,
            resolution=resolution, captured_at=CAPTURE, recorded_at=CAPTURE)


def test_append_rejects_same_day_or_unknown_qualified_bound_before_sql():
    resolution = derive()
    resolution["effective_available_at"] = datetime(2024,2,2,14,30,tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="bound"):
        api().append_daily_publication_evidence(None, document=document(), filing=filing(),
            resolution=resolution, captured_at=CAPTURE, recorded_at=CAPTURE)
