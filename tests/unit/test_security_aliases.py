from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from backtest_data.catalog.aliases import (
    ALIAS_POLICY_VERSION, alias_policy, normalize_alias, resolve_records,
)


def instant(day=1):
    return datetime(2024, 1, day, tzinfo=timezone.utc)


def record(identifier="005930", security_id="common", **changes):
    evidence = {
        "alias_policy_version": ALIAS_POLICY_VERSION,
        "source_assertion_version": alias_policy()["source_assertion_version"],
        "namespace": "universal", "normalized_key": normalize_alias(identifier),
        "source": "fixture", "provenance": {"source_id": "directory"},
        "assertion_key": security_id + ":" + identifier,
        "status": "accepted", "recorded_at": instant().isoformat(),
    }
    evidence.update(changes.pop("evidence", {}))
    values = dict(id=security_id + ":" + identifier, security_id=security_id,
                  identifier=identifier, identifier_type="ticker", venue="KOSPI",
                  valid_from=date(2024, 1, 1), valid_to=None,
                  available_at=instant(), captured_at=instant(), evidence=evidence)
    values.update(changes)
    return SimpleNamespace(**values)


SECURITIES = {
    "common": {"issuer_id": "issuer", "name": "Samsung common", "market": "KR"},
    "preferred": {"issuer_id": "issuer", "name": "Samsung preferred", "market": "KR"},
    "us": {"issuer_id": None, "name": "Samsung depositary", "market": "US"},
}


def resolve(value, rows, **options):
    return resolve_records(value, rows, SECURITIES, as_of=instant(20),
                           history_mode="observed_system", **options)


@pytest.mark.parametrize(("value", "expected"), [
    ("  Ｓａｍｓｕｎｇ\u3000 Electronics\t", "samsung electronics"),
    ("005930", "005930"), (" A005930 ", "a005930"),
    ("BRK.B", "brk.b"), ("BRK-B", "brk-b"), ("Straße", "strasse"),
])
def test_normalization_preserves_codes_and_class_punctuation(value, expected):
    assert normalize_alias(value) == expected
    assert alias_policy()["unicode_version"] == "15.1.0"


def test_exact_aliases_share_one_security_without_fuzzy_matching():
    aliases = ["005930", "A005930", "삼성전자", "Samsung Electronics"]
    rows = [record(alias) for alias in aliases]
    for alias in aliases:
        result = resolve(alias, rows)
        assert result["status"] == "resolved"
        assert result["security_id"] == "common"
        assert result["candidates"][0]["matched_identifiers"][0]["provenance"]
    assert resolve("Samsng Electronics", rows)["status"] == "unresolved"
    assert resolve("Samung Electronics", rows)["status"] == "unresolved"
    assert resolve("Samsung common", rows)["status"] == "unresolved"


def test_multiple_alias_assertions_deduplicate_security_but_company_names_can_be_ambiguous():
    rows = [record("Samsung", "common"), record("SAMSUNG", "common"),
            record("Samsung", "preferred"), record("Samsung", "us", venue="NYSE")]
    result = resolve("samsung", rows)
    assert result["status"] == "ambiguous"
    assert "security_id" not in result
    assert {c["security_id"] for c in result["candidates"]} == set(SECURITIES)
    assert resolve("samsung", rows, market="us")["security_id"] == "us"
    assert resolve("samsung", rows, venue="NYSE")["security_id"] == "us"
    assert resolve("samsung", rows, namespace="missing")["status"] == "unresolved"


def test_effective_interval_is_half_open_and_defaults_to_as_of_date():
    rows = [record(valid_to=date(2024, 1, 10))]
    assert resolve("005930", rows, session_date=date(2024, 1, 9))["status"] == "resolved"
    assert resolve("005930", rows, session_date=date(2024, 1, 10))["status"] == "unresolved"
    assert resolve("005930", rows, session_date=date(2023, 12, 31))["status"] == "unresolved"
    assert resolve("005930", rows)["status"] == "unresolved"


def test_latest_visible_assertion_is_chosen_before_range_status_and_identifier_filters():
    original = record(evidence={"assertion_key": "correction"})
    corrected = record("005935", "preferred", id="correction-2", available_at=instant(5),
                       captured_at=instant(5), evidence={"assertion_key": "correction", "recorded_at": instant(5).isoformat()})
    assert resolve("005930", [original, corrected])["status"] == "unresolved"
    assert resolve("005935", [original, corrected])["security_id"] == "preferred"
    withdrawn = record(id="withdrawal", valid_from=date(2024, 2, 1), available_at=instant(6),
                       captured_at=instant(6), evidence={"assertion_key": "correction", "status": "withdrawn", "recorded_at": instant(6).isoformat()})
    assert resolve("005930", [original, withdrawn])["status"] == "unresolved"
    cutoff = resolve_records("005930", [original, withdrawn], SECURITIES,
                             as_of=instant(3), history_mode="observed_system")
    assert cutoff["security_id"] == "common"


@pytest.mark.parametrize("change", [
    {"available_at": instant(21)}, {"captured_at": instant(21)},
    {"evidence": {"recorded_at": instant(21).isoformat()}},
    {"evidence": {"alias_policy_version": "future-policy"}},
    {"evidence": {"source_assertion_version": "unknown-source-v9"}},
])
def test_future_or_unrecognized_evidence_fails_closed(change):
    assert resolve("005930", [record(**change)])["status"] == "unresolved"


def test_invalid_latest_policy_suppresses_previous_assertion():
    old = record(evidence={"assertion_key": "same"})
    new = record(id="new", available_at=instant(5), evidence={"assertion_key": "same", "alias_policy_version": "unknown"})
    assert resolve("005930", [old, new])["status"] == "unresolved"


def test_public_mode_uses_availability_and_recording_but_observed_mode_also_uses_capture():
    row = record(captured_at=instant(21))
    result = resolve_records("005930", [row], SECURITIES, as_of=instant(20),
                             history_mode="reconstructed_public")
    assert result["security_id"] == "common"
    assert resolve("005930", [row])["status"] == "unresolved"


@pytest.mark.parametrize("options", [
    {"as_of": datetime(2024, 1, 20)}, {"history_mode": "unknown"},
    {"session_date": "2024-01-01"}, {"session_date": instant()},
])
def test_invalid_temporal_options_are_rejected(options):
    values = {"as_of": instant(20), "history_mode": "observed_system"}
    values.update(options)
    with pytest.raises(ValueError):
        resolve_records("005930", [record()], SECURITIES, **values)


@pytest.mark.parametrize("change", [
    {"valid_to": date(2024, 1, 1)}, {"valid_from": "2024-01-01"},
    {"captured_at": datetime(2024, 1, 1)},
])
def test_invalid_row_dates_and_timestamps_are_rejected(change):
    with pytest.raises(ValueError):
        resolve("005930", [record(**change)])


def test_effective_default_uses_market_local_date_and_future_dates_are_rejected():
    cutoff = datetime(2024, 1, 1, 23, tzinfo=timezone.utc)
    kr = record(valid_from=date(2024, 1, 2))
    result = resolve_records("005930", [kr], SECURITIES, as_of=cutoff, history_mode="observed_system")
    assert result["security_id"] == "common"
    assert result["candidates"][0]["session_date"] == "2024-01-02"
    us = record("US", "us", valid_from=date(2024, 1, 2))
    assert resolve_records("US", [us], SECURITIES, as_of=cutoff,
                           history_mode="observed_system")["status"] == "unresolved"
    with pytest.raises(ValueError, match="future"):
        resolve_records("005930", [kr], SECURITIES, as_of=cutoff, history_mode="observed_system",
                        session_date=date(2024, 1, 3), market="KR")
