from datetime import date, datetime, timezone

import pytest

from backtest_data.catalog.calendar import date_only_available_at


def test_date_only_uses_next_real_session_not_weekday_guess():
    sessions = [
        {"session": date(2024, 12, 24), "open_at": datetime(2024, 12, 24, 14, 30, tzinfo=timezone.utc)},
        {"session": date(2024, 12, 26), "open_at": datetime(2024, 12, 26, 14, 30, tzinfo=timezone.utc)},
    ]
    assert date_only_available_at(date(2024, 12, 24), sessions) == sessions[1]["open_at"]
    with pytest.raises(ValueError, match="calendar"):
        date_only_available_at(date(2025, 1, 1), sessions)
