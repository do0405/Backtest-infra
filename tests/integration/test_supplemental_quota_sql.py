from datetime import datetime, timedelta, timezone
import pytest

def test_alpha_rolling_quota_survives_restarts_and_counts_failed_attempts(db_session):
    from backtest_data.supplemental.quota import reserve_alpha_call
    from backtest_data.ingestion.http import ProviderError
    now = datetime(2037, 1, 1, tzinfo=timezone.utc)
    for _ in range(25): reserve_alpha_call(db_session, now)
    with pytest.raises(ProviderError, match='free_rolling_daily_quota_deferred'):
        reserve_alpha_call(db_session, now + timedelta(hours=23))
    reserve_alpha_call(db_session, now + timedelta(days=1, seconds=1))
