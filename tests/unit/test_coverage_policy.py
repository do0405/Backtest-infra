from datetime import date, datetime, timezone
import importlib

import pytest


def policy():
    return importlib.import_module("backtest_data.coverage.policy")


def test_weekday_grid_is_provisional_and_does_not_drop_public_holidays():
    days = policy().provisional_weekdays(date(2026, 1, 1), date(2026, 1, 5))
    assert days == (date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 5))
    assert policy().missing_date_spans(days, {date(2026, 1, 2)}) == [
        {"start": "2026-01-01", "end": "2026-01-01", "count": 1},
        {"start": "2026-01-05", "end": "2026-01-05", "count": 1},
    ]


@pytest.mark.parametrize("kind", ["etf", "ETF", "etn", "ETN"])
def test_only_explicit_fund_classification_is_not_applicable(kind):
    assert policy().company_fundamentals_applicability(kind) == "not_applicable"


@pytest.mark.parametrize("kind", ["unclassified", "etp_unknown", "equity_or_note_unknown"])
def test_ambiguous_classification_remains_unknown(kind):
    assert policy().company_fundamentals_applicability(kind) == "unknown"


def test_bounds_reject_naive_or_future_dates_and_oversized_windows():
    with pytest.raises(ValueError, match="aware"):
        policy().validate_bounds(date(2016, 1, 1), date(2026, 10, 7), datetime(2026, 10, 10))
    with pytest.raises(ValueError, match="future"):
        policy().validate_bounds(date(2026, 10, 11), date(2026, 10, 12),
            datetime(2026, 10, 10, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="bounded"):
        policy().validate_bounds(date(1990, 1, 1), date(2026, 10, 7),
            datetime(2026, 10, 10, tzinfo=timezone.utc))


def test_cutoff_is_exclusive_of_the_next_local_calendar_day():
    assert policy().market_cutoff(date(2026, 10, 7), "KR") < datetime(2026, 10, 7, 15, tzinfo=timezone.utc)
    assert policy().market_cutoff(date(2026, 10, 7), "US") < datetime(2026, 10, 8, 4, tzinfo=timezone.utc)
