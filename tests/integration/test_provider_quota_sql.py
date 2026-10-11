from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from backtest_data.db import new_id, transaction, utcnow
from backtest_data.ingestion.jobs import RateReservationDeferred, reserve_rate_slot
from backtest_data.ingestion.models import RateBudget

pytestmark = pytest.mark.integration


def legacy_reserve(session, key, now, interval=0.001, max_delay=30):
    """The pre-policy executable's reservation code, without the new preflight."""
    session.execute(insert(RateBudget).values(key=key, next_available_at=now)
        .on_conflict_do_nothing(index_elements=[RateBudget.key]))
    session.expire_all()  # Existing HTTP opens a fresh session for every request.
    budget = session.scalar(select(RateBudget).where(RateBudget.key == key).with_for_update())
    available = max(now, budget.next_available_at)
    if (available - now).total_seconds() > max_delay:
        raise RateReservationDeferred('Source budget deferred before reservation')
    budget.next_available_at = available + timedelta(seconds=interval)
    session.flush()
    return available


def setup_policy(session, *, source='krx', floor=0.01, cap=2, window='calendar_day', **kwargs):
    from backtest_data.ingestion.quota import install_quota_controls, upsert_quota_policy
    install_quota_controls(session)
    key = f'{source}:{new_id()}'
    upsert_quota_policy(session, key, minimum_interval_seconds=floor,
        window_kind=window, credit_limit=cap, **kwargs)
    return key


def usage(session, key):
    return session.scalar(text('SELECT count(*) FROM provider_quota_reservation WHERE key=:key'), {'key': key})


def test_legacy_reservation_arms_calendar_reset_on_final_credit(db_session):
    key = setup_policy(db_session)
    now = utcnow()
    legacy_reserve(db_session, key, now)
    legacy_reserve(db_session, key, now)
    assert usage(db_session, key) == 2
    deadline = db_session.scalar(select(RateBudget.next_available_at).where(RateBudget.key == key))
    expected = db_session.scalar(text("SELECT ((clock_timestamp() AT TIME ZONE 'Asia/Seoul')::date + 1)::timestamp AT TIME ZONE 'Asia/Seoul'"))
    assert deadline >= expected
    for _ in range(3):
        with pytest.raises(RateReservationDeferred):
            legacy_reserve(db_session, key, now)
    assert usage(db_session, key) == 2


def test_idle_floor_clamps_from_old_or_server_clock_without_double_gap(db_session):
    from backtest_data.ingestion.quota import upsert_quota_policy
    key = f'alpaca:{new_id()}'
    db_session.add(RateBudget(key=key, next_available_at=utcnow() - timedelta(hours=1)))
    db_session.flush()
    upsert_quota_policy(db_session, key, minimum_interval_seconds=0.334)
    before = db_session.scalar(select(func.clock_timestamp()))
    reserve_rate_slot(db_session, key, utcnow(), 0.334)
    deadline = db_session.scalar(select(RateBudget.next_available_at).where(RateBudget.key == key))
    after = db_session.scalar(select(func.clock_timestamp()))
    assert before + timedelta(seconds=0.334) <= deadline <= after + timedelta(seconds=0.334)


def test_repeated_reservation_refreshes_trigger_clock_even_with_cached_orm_row(db_session):
    key = setup_policy(db_session, source='alpaca', floor=0.334, cap=None, window='none')
    cached_budget = db_session.get(RateBudget, key)
    first = reserve_rate_slot(db_session, key, utcnow(), 0.001)
    second = reserve_rate_slot(db_session, key, utcnow(), 0.001)
    assert second >= first + timedelta(seconds=0.334)
    assert cached_budget.key == key


def test_rolling_guard_arms_oldest_reservation_expiry(db_session):
    key = setup_policy(db_session, source='opendart', cap=2, window='rolling_24h')
    reserve_rate_slot(db_session, key, utcnow(), 0.001)
    reserve_rate_slot(db_session, key, utcnow(), 0.001)
    oldest = db_session.scalar(text('SELECT min(reserved_at) FROM provider_quota_reservation WHERE key=:key'), {'key': key})
    deadline = db_session.scalar(select(RateBudget.next_available_at).where(RateBudget.key == key))
    assert deadline >= oldest + timedelta(hours=24)
    with pytest.raises(RateReservationDeferred):
        reserve_rate_slot(db_session, key, utcnow(), 0.001, max_delay_seconds=30)
    assert usage(db_session, key) == 2


def test_only_updates_charge_and_explicit_cooldown_does_not(db_session):
    from backtest_data.ingestion.quota import set_provider_cooldown
    key = setup_policy(db_session, cap=10)
    reserve_rate_slot(db_session, key, utcnow(), 0.001)
    reserve_rate_slot(db_session, key, utcnow(), 0.001)
    assert usage(db_session, key) == 2  # ON CONFLICT insert must not charge.
    until = utcnow() + timedelta(hours=2)
    set_provider_cooldown(db_session, key, until)
    assert usage(db_session, key) == 2
    # A legacy cooldown cannot identify itself; conservative charge is safe.
    db_session.execute(update(RateBudget).where(RateBudget.key == key).values(next_available_at=until + timedelta(hours=1)))
    assert usage(db_session, key) == 3
    set_provider_cooldown(db_session, key, utcnow())
    assert db_session.scalar(select(RateBudget.next_available_at).where(RateBudget.key == key)) >= until + timedelta(hours=1)


def test_korean_date_bucket_uses_policy_timezone_not_session(db_session):
    key = setup_policy(db_session)
    db_session.execute(text("SET LOCAL TIME ZONE 'America/New_York'"))
    future = utcnow() + timedelta(days=2)
    boundary = datetime(future.year, future.month, future.day, 14, 59, 59, tzinfo=timezone.utc)
    from backtest_data.ingestion.quota import set_provider_cooldown
    set_provider_cooldown(db_session, key, boundary)
    reserve_rate_slot(db_session, key, boundary, 0.001)
    reserve_rate_slot(db_session, key, boundary, 0.001)
    deadline = db_session.scalar(select(RateBudget.next_available_at).where(RateBudget.key == key))
    assert deadline.astimezone(timezone.utc) == boundary.replace(hour=15, minute=0, second=0)


def test_snapshot_restore_preserves_owned_cooldown_and_deduplicates_credits(db_session):
    from backtest_data.remote.codec import dumps, loads
    from backtest_data.ingestion.quota import snapshot_rate_state, restore_rate_state, set_provider_cooldown
    key = setup_policy(db_session, cap=10)
    reserve_rate_slot(db_session, key, utcnow(), 0.001)
    deadline = utcnow() + timedelta(hours=6)
    set_provider_cooldown(db_session, key, deadline)
    payload = snapshot_rate_state(db_session, ['krx'], binding={'stream': 'owned'})
    payload = loads(dumps(payload))
    restore_rate_state(db_session, payload, sources=['krx'], binding={'stream': 'owned'})
    restore_rate_state(db_session, payload, sources=['krx'], binding={'stream': 'owned'})
    assert usage(db_session, key) == 1
    assert db_session.scalar(select(RateBudget.next_available_at).where(RateBudget.key == key)) >= deadline


def test_allocation_requires_credits_before_legacy_admission(db_session):
    from backtest_data.ingestion.quota import set_allocation_day_limits
    key = setup_policy(db_session, require_allocation=True, cap=9500)
    with pytest.raises(RateReservationDeferred):
        reserve_rate_slot(db_session, key, utcnow(), 0.001, max_delay_seconds=30)
    day = db_session.scalar(text("SELECT (clock_timestamp() AT TIME ZONE 'Asia/Seoul')::date"))
    set_allocation_day_limits(db_session, key, {day: 2}, binding_hash='a' * 64)
    reserve_rate_slot(db_session, key, utcnow(), 0.001, max_delay_seconds=30)
    reserve_rate_slot(db_session, key, utcnow(), 0.001, max_delay_seconds=30)
    with pytest.raises(RateReservationDeferred):
        reserve_rate_slot(db_session, key, utcnow(), 0.001, max_delay_seconds=30)
    assert usage(db_session, key) == 2


def test_fresh_grant_adds_to_consumed_only_and_preserves_provider_cooldown(db_session):
    from backtest_data.ingestion.quota import grant_allocation_day_credits, set_provider_cooldown
    key = setup_policy(db_session, require_allocation=True, cap=9500)
    day = db_session.scalar(text("SELECT (clock_timestamp() AT TIME ZONE 'Asia/Seoul')::date"))
    grant_allocation_day_credits(db_session, key, {day: 2}, binding_hash='a' * 64)
    reserve_rate_slot(db_session, key, utcnow(), 0.001)
    reserve_rate_slot(db_session, key, utcnow(), 0.001)
    provider_deadline = utcnow() + timedelta(hours=2)
    set_provider_cooldown(db_session, key, provider_deadline)
    grant_allocation_day_credits(db_session, key, {day: 1}, binding_hash='b' * 64)
    cap = db_session.scalar(text('SELECT credit_limit FROM provider_quota_allocation_day WHERE key=:key'), {'key': key})
    assert cap == 3
    assert db_session.scalar(select(RateBudget.next_available_at).where(RateBudget.key == key)) == provider_deadline
    with pytest.raises(RateReservationDeferred):
        reserve_rate_slot(db_session, key, utcnow(), 0.001, max_delay_seconds=30)
    assert usage(db_session, key) == 2


def test_unallocated_first_request_and_day_rollover_make_zero_http(db_session, monkeypatch):
    import httpx
    from contextlib import contextmanager
    from backtest_data.ingestion.http import ProviderHTTP, ProviderError
    import backtest_data.ingestion.http as module
    key = setup_policy(db_session, require_allocation=True, cap=9500)
    from backtest_data.ingestion.quota import set_allocation_day_limits
    calls = []
    @contextmanager
    def tx(engine):
        yield db_session
    monkeypatch.setattr(module, 'transaction', tx)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200)))
    transport = ProviderHTTP(None, client=client, budget_scope=key.split(':', 1)[1])
    try:
        clocks = (utcnow(), utcnow(), utcnow() + timedelta(days=1))
        for index, clock in enumerate(clocks):
            if index == 1:
                day = db_session.scalar(text("SELECT (clock_timestamp() AT TIME ZONE 'Asia/Seoul')::date"))
                set_allocation_day_limits(db_session, key, {day: 0}, binding_hash='a' * 64)
            monkeypatch.setattr(module, 'utcnow', lambda: clock)
            with pytest.raises(ProviderError, match='rate_budget_deferred'):
                transport.request('krx', 'GET', 'https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd')
        assert calls == []
        assert usage(db_session, key) == 0
    finally:
        transport.close()


def test_restored_allocation_history_cannot_authorize_a_fresh_run(db_session):
    from backtest_data.ingestion.quota import grant_allocation_day_credits, snapshot_rate_state, restore_rate_state
    key = setup_policy(db_session, require_allocation=True, cap=9500)
    day = db_session.scalar(text("SELECT (clock_timestamp() AT TIME ZONE 'Asia/Seoul')::date"))
    grant_allocation_day_credits(db_session, key, {day: 2}, binding_hash='a' * 64)
    reserve_rate_slot(db_session, key, utcnow(), 0.001)
    payload = snapshot_rate_state(db_session, ['krx'], binding={'stream': 'owned'})
    restore_rate_state(db_session, payload, sources=['krx'], binding={'stream': 'owned'})
    with pytest.raises(RateReservationDeferred):
        reserve_rate_slot(db_session, key, utcnow(), 0.001, max_delay_seconds=30)
    assert usage(db_session, key) == 1
    grant_allocation_day_credits(db_session, key, {day: 1}, binding_hash='b' * 64)
    reserve_rate_slot(db_session, key, utcnow(), 0.001, max_delay_seconds=30)
    with pytest.raises(RateReservationDeferred):
        reserve_rate_slot(db_session, key, utcnow(), 0.001, max_delay_seconds=30)
    assert usage(db_session, key) == 2


def test_failed_and_retried_http_each_charge_before_network(db_session, monkeypatch):
    import httpx
    from contextlib import contextmanager
    import backtest_data.ingestion.http as module
    key = setup_policy(db_session, source='opendart', cap=3, window='rolling_24h')
    calls = []
    @contextmanager
    def tx(engine):
        yield db_session
    def respond(request):
        calls.append(usage(db_session, key))
        return httpx.Response(503 if len(calls) < 3 else 200)
    monkeypatch.setattr(module, 'transaction', tx)
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    client = httpx.Client(transport=httpx.MockTransport(respond))
    transport = module.ProviderHTTP(None, client=client, budget_scope=key.split(':', 1)[1])
    try:
        transport.request('opendart', 'GET', 'https://opendart.fss.or.kr/api/list.json')
        assert calls == [1, 2, 3]
        with pytest.raises(module.ProviderError, match='rate_budget_deferred'):
            transport.request('opendart', 'GET', 'https://opendart.fss.or.kr/api/list.json')
        assert calls == [1, 2, 3]
        assert usage(db_session, key) == 3
    finally:
        transport.close()


def test_concurrent_old_workers_share_one_committed_allowance(pg_engine):
    with transaction(pg_engine) as session:
        key = setup_policy(session, cap=4)
    def reserve(_):
        try:
            with transaction(pg_engine) as session:
                legacy_reserve(session, key, utcnow())
            return True
        except RateReservationDeferred:
            return False
    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            assert sum(executor.map(reserve, range(12))) == 4
        with transaction(pg_engine) as session:
            assert usage(session, key) == 4
    finally:
        with transaction(pg_engine) as session:
            session.execute(text('DELETE FROM provider_quota_reservation WHERE key=:key'), {'key': key})
            session.execute(text('DELETE FROM provider_quota_policy WHERE key=:key'), {'key': key})
            session.execute(text('DELETE FROM rate_budget WHERE key=:key'), {'key': key})
