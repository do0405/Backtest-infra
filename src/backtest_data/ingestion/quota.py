"""Shared provider admission and portable, owned operational rate state."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
import math
import re
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from ..db import utcnow
from .models import RateBudget
from .quota_models import ProviderQuotaAllocationDay, ProviderQuotaPolicy, ProviderQuotaReservation
from .quota_sql import install_quota_schema

DEFAULT_POLICIES = {
    'alpaca:shared_account': {'minimum_interval_seconds': 0.334, 'window_kind': 'none', 'credit_limit': None},
    'krx:shared_account': {'minimum_interval_seconds': 10.0, 'window_kind': 'calendar_day', 'credit_limit': 9500},
    # Project guard only: the account entitlement and reset remain unverified.
    'opendart:shared_account': {'minimum_interval_seconds': 0.5, 'window_kind': 'rolling_24h', 'credit_limit': 19000},
}


def _aware(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError('absolute timezone-aware rate clock required') from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('absolute timezone-aware rate clock required')
    return result


def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}:[A-Za-z0-9_.-]{1,39}', value):
        raise ValueError('valid provider budget key required')
    return value


def _policy_values(*, minimum_interval_seconds, window_kind='none', credit_limit=None,
                   timezone='Asia/Seoul', require_allocation=False):
    if isinstance(minimum_interval_seconds, bool) or not isinstance(minimum_interval_seconds, (int, float, Decimal)) or not math.isfinite(minimum_interval_seconds) or minimum_interval_seconds <= 0:
        raise ValueError('positive finite source interval required')
    if window_kind not in {'none', 'calendar_day', 'rolling_24h'}:
        raise ValueError('valid quota window required')
    if (window_kind == 'none' and credit_limit is not None) or (window_kind != 'none' and (type(credit_limit) is not int or credit_limit <= 0)):
        raise ValueError('positive quota credit limit required')
    if type(require_allocation) is not bool or require_allocation and window_kind != 'calendar_day':
        raise ValueError('allocation requires calendar-day policy')
    try:
        ZoneInfo(timezone)
    except (ValueError, TypeError, KeyError):
        raise ValueError('valid quota timezone required') from None
    return dict(minimum_interval_seconds=float(minimum_interval_seconds), window_kind=window_kind,
        credit_limit=credit_limit, timezone=timezone, require_allocation=require_allocation)


@contextmanager
def _suppress_charge(session):
    previous = session.scalar(text("SELECT current_setting('backtest.quota_suppress', true)")) or ''
    session.execute(text("SELECT set_config('backtest.quota_suppress', 'on', true)"))
    try:
        yield
    finally:
        session.execute(text("SELECT set_config('backtest.quota_suppress', :previous, true)"), {'previous': previous})


def _lock_budget(session, key):
    session.execute(insert(RateBudget).values(key=key, next_available_at=func.clock_timestamp())
        .on_conflict_do_nothing(index_elements=[RateBudget.key]))
    return session.scalar(select(RateBudget).where(RateBudget.key == key).with_for_update()
        .execution_options(populate_existing=True))


def _quota_window(session, policy, clock):
    if policy.window_kind == 'none':
        return 0, None, None
    statement = select(func.count(ProviderQuotaReservation.id), func.min(ProviderQuotaReservation.reserved_at)).where(
        ProviderQuotaReservation.key == policy.key)
    cap = policy.credit_limit
    if policy.window_kind == 'calendar_day':
        local_day = clock.astimezone(ZoneInfo(policy.timezone)).date()
        start = datetime.combine(local_day, datetime.min.time(), ZoneInfo(policy.timezone))
        reset = start + timedelta(days=1)
        statement = statement.where(ProviderQuotaReservation.reserved_at >= start, ProviderQuotaReservation.reserved_at < reset)
        if policy.require_allocation:
            allocated = session.scalar(select(ProviderQuotaAllocationDay.credit_limit).where(
                ProviderQuotaAllocationDay.key == policy.key, ProviderQuotaAllocationDay.allocation_date == local_day))
            cap = min(cap, allocated or 0)
        used, _ = session.execute(statement).one()
    else:
        used, oldest = session.execute(statement.where(ProviderQuotaReservation.reserved_at > clock - timedelta(hours=24))).one()
        reset = (oldest or clock) + timedelta(hours=24)
    return used, cap, reset


def _reconcile_gate(session, budget, policy, *, release_allocation=False):
    now = session.scalar(select(func.clock_timestamp()))
    used, cap, reset = _quota_window(session, policy, now)
    exhausted = cap is not None and used >= cap
    deadline = budget.next_available_at
    if release_allocation and not exhausted and policy.blocked_until is not None and deadline == policy.blocked_until:
        # Release only a recorded quota gate. Provider Retry-After and DART 020
        # remain absolute and are never reset by a newly allocated run.
        deadline = max(now, policy.cooldown_until or now)
    if exhausted:
        deadline = max(deadline, reset)
        policy.blocked_until = reset
    elif release_allocation:
        policy.blocked_until = None
    if policy.cooldown_until is not None:
        deadline = max(deadline, policy.cooldown_until)
    session.flush()
    if deadline != budget.next_available_at:
        with _suppress_charge(session):
            session.execute(update(RateBudget).where(RateBudget.key == budget.key).values(next_available_at=deadline))
        session.expire(budget)


def upsert_quota_policy(session, key, *, minimum_interval_seconds, window_kind='none', credit_limit=None,
                        timezone='Asia/Seoul', require_allocation=False):
    key = _key(key)
    values = _policy_values(minimum_interval_seconds=minimum_interval_seconds, window_kind=window_kind,
        credit_limit=credit_limit, timezone=timezone, require_allocation=require_allocation)
    budget = _lock_budget(session, key)  # Same lock order as legacy UPDATE trigger.
    session.execute(insert(ProviderQuotaPolicy).values(key=key, **values).on_conflict_do_update(
        index_elements=[ProviderQuotaPolicy.key], set_=values))
    policy = session.scalar(select(ProviderQuotaPolicy).where(ProviderQuotaPolicy.key == key).with_for_update()
        .execution_options(populate_existing=True))
    _reconcile_gate(session, budget, policy)
    return policy


def install_quota_controls(session, *, lock_timeout='2s', statement_timeout='15s', defaults=True):
    """Explicit additive installer suitable for live activation, without upgrade head."""
    install_quota_schema(session.connection(), lock_timeout=lock_timeout, statement_timeout=statement_timeout)
    if defaults:
        for key, values in DEFAULT_POLICIES.items():
            # Reinstallation must not replace an allocation fence or tuned policy.
            if session.scalar(select(ProviderQuotaPolicy.key).where(ProviderQuotaPolicy.key == key)) is None:
                upsert_quota_policy(session, key, **values)


def quota_preflight(session, key, now):
    """Reject exhausted/unallocated admission before computing any HTTP slot.

    Caller must own the rate_budget row lock before this policy lock, matching
    legacy UPDATE trigger lock order. reserve_rate_slot is the public seam.
    """
    from .jobs import RateReservationDeferred
    now = _aware(now)
    policy = session.scalar(select(ProviderQuotaPolicy).where(ProviderQuotaPolicy.key == key).with_for_update())
    if policy is None or policy.window_kind == 'none':
        return
    server_now = session.scalar(select(func.clock_timestamp()))
    used, cap, _ = _quota_window(session, policy, max(now, server_now))
    if used >= cap:
        raise RateReservationDeferred('Source quota deferred before reservation')


def set_provider_cooldown(session, key, until):
    key, until = _key(key), _aware(until)
    budget = _lock_budget(session, key)
    policy = session.scalar(select(ProviderQuotaPolicy).where(ProviderQuotaPolicy.key == key).with_for_update())
    if policy is not None:
        policy.cooldown_until = max(until, policy.cooldown_until or until)
        session.flush()
    with _suppress_charge(session):
        session.execute(update(RateBudget).where(RateBudget.key == key).values(
            next_available_at=func.greatest(RateBudget.next_available_at, until)))
    session.expire(budget)


def set_allocation_day_limits(session, key, limits, *, binding_hash):
    """Replace a run's allocation ceilings; consumed reservations never refund."""
    key = _key(key)
    if not isinstance(limits, dict) or not isinstance(binding_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', binding_hash):
        raise ValueError('bound allocation day limits required')
    normalized = {}
    for day, cap in limits.items():
        day = date.fromisoformat(day) if isinstance(day, str) else day
        if type(day) is not date or type(cap) is not int or cap < 0:
            raise ValueError('nonnegative allocation day credit limit required')
        normalized[day] = cap
    budget = _lock_budget(session, key)
    policy = session.scalar(select(ProviderQuotaPolicy).where(ProviderQuotaPolicy.key == key).with_for_update())
    if policy is None or policy.window_kind != 'calendar_day' or not policy.require_allocation:
        raise ValueError('explicit allocation calendar policy required')
    session.query(ProviderQuotaAllocationDay).filter(ProviderQuotaAllocationDay.key == key).delete(synchronize_session=False)
    session.add_all(ProviderQuotaAllocationDay(key=key, allocation_date=day, credit_limit=cap, binding_hash=binding_hash)
        for day, cap in normalized.items())
    session.flush()
    _reconcile_gate(session, budget, policy, release_allocation=True)


def grant_allocation_day_credits(session, key, credits_by_day, *, binding_hash):
    """Translate a fresh durable grant into consumed-plus-granted SQL ceilings."""
    key = _key(key)
    if not isinstance(credits_by_day, dict):
        raise ValueError('bound allocation day credits required')
    _lock_budget(session, key)
    policy = session.scalar(select(ProviderQuotaPolicy).where(ProviderQuotaPolicy.key == key).with_for_update())
    if policy is None or policy.window_kind != 'calendar_day' or not policy.require_allocation:
        raise ValueError('explicit allocation calendar policy required')
    limits = {}
    for day, credits in credits_by_day.items():
        day = date.fromisoformat(day) if isinstance(day, str) else day
        if type(day) is not date or type(credits) is not int or credits < 0:
            raise ValueError('nonnegative allocation day credits required')
        start = datetime.combine(day, datetime.min.time(), ZoneInfo(policy.timezone))
        consumed = session.scalar(select(func.count(ProviderQuotaReservation.id)).where(
            ProviderQuotaReservation.key == key, ProviderQuotaReservation.reserved_at >= start,
            ProviderQuotaReservation.reserved_at < start + timedelta(days=1)))
        limits[day] = consumed + credits
    set_allocation_day_limits(session, key, limits, binding_hash=binding_hash)


def _owned_keys(sources):
    result = sorted(set(sources))
    if not result or any(not isinstance(source, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', source) for source in result):
        raise ValueError('explicit owned sources required')
    return result


def validate_rate_state(payload, *, sources, binding):
    sources = _owned_keys(sources)
    fields = {'version', 'binding', 'sources', 'policies', 'rate_budgets', 'reservations', 'allocation_days'}
    if not isinstance(payload, dict) or set(payload) != fields or payload['version'] != 1:
        raise ValueError('invalid portable rate state')
    if payload['binding'] != binding:
        raise ValueError('portable rate state binding mismatch')
    if not isinstance(payload['sources'], list) or sorted(payload['sources']) != sources:
        raise ValueError('rate state outside explicitly owned sources')
    row_fields = {
        'policies': {'key', 'minimum_interval_seconds', 'window_kind', 'credit_limit', 'timezone', 'require_allocation', 'blocked_until', 'cooldown_until'},
        'rate_budgets': {'key', 'next_available_at'},
        'reservations': {'id', 'key', 'reserved_at'},
        'allocation_days': {'key', 'allocation_date', 'credit_limit', 'binding_hash'},
    }
    for table, required in row_fields.items():
        if not isinstance(payload[table], list):
            raise ValueError('invalid portable rate rows')
        identities = set()
        for row in payload[table]:
            if not isinstance(row, dict) or set(row) != required:
                raise ValueError('invalid portable rate row')
            key = _key(row['key'])
            if key.split(':', 1)[0] not in sources:
                raise ValueError('rate key outside explicitly owned source')
            identity = (key, row.get('id'), row.get('allocation_date'))
            if identity in identities:
                raise ValueError('duplicate portable rate row')
            identities.add(identity)
            for name in ('next_available_at', 'reserved_at', 'blocked_until', 'cooldown_until'):
                if name in row and row[name] is not None:
                    _aware(row[name])
            if table == 'policies':
                _policy_values(**{name: row[name] for name in required - {'key', 'blocked_until', 'cooldown_until'}})
            if table == 'reservations' and (not isinstance(row['id'], str) or not re.fullmatch(r'[0-9a-f-]{36}', row['id'])):
                raise ValueError('invalid reservation identity')
            if table == 'allocation_days':
                date.fromisoformat(row['allocation_date'])
                if type(row['credit_limit']) is not int or row['credit_limit'] < 0 or not re.fullmatch(r'[0-9a-f]{64}', row['binding_hash']):
                    raise ValueError('invalid bound allocation row')
    policy_keys = {row['key'] for row in payload['policies']}
    if any(row['key'] not in policy_keys for row in payload['reservations'] + payload['allocation_days']):
        raise ValueError('portable quota state requires its policy')
    return payload


def snapshot_rate_state(session, sources, *, binding):
    sources = _owned_keys(sources)
    payload = {'version': 1, 'binding': dict(binding), 'sources': sources}
    for name, model in (('policies', ProviderQuotaPolicy), ('rate_budgets', RateBudget),
                        ('reservations', ProviderQuotaReservation), ('allocation_days', ProviderQuotaAllocationDay)):
        query = select(model).where(func.split_part(model.key, ':', 1).in_(sources)).order_by(model.key)
        if name == 'reservations':
            query = query.where(model.reserved_at >= func.clock_timestamp() - timedelta(hours=48)).order_by(model.reserved_at, model.id)
        payload[name] = [{column.name: value.isoformat() if isinstance(value := getattr(row, column.name), (date, datetime)) else value
            for column in model.__table__.columns} for row in session.scalars(query)]
        if name == 'policies':
            for row in payload[name]:
                row['minimum_interval_seconds'] = Decimal(str(row['minimum_interval_seconds']))
    return validate_rate_state(payload, sources=sources, binding=binding)


def restore_rate_state(session, payload, *, sources, binding):
    """Max-merge clocks and consumed credits. Allocations require fresh run binding."""
    validate_rate_state(payload, sources=sources, binding=binding)
    for row in payload['policies']:
        values = {name: row[name] for name in ('minimum_interval_seconds', 'window_kind', 'credit_limit', 'timezone', 'require_allocation')}
        budget = _lock_budget(session, row['key'])
        current = session.scalar(select(ProviderQuotaPolicy).where(ProviderQuotaPolicy.key == row['key']).with_for_update())
        if current is not None:
            if (current.window_kind, current.timezone) != (row['window_kind'], row['timezone']):
                raise ValueError('portable rate policy window mismatch')
            values['minimum_interval_seconds'] = max(current.minimum_interval_seconds, values['minimum_interval_seconds'])
            values['require_allocation'] = current.require_allocation or values['require_allocation']
            if current.credit_limit is not None:
                values['credit_limit'] = min(current.credit_limit, values['credit_limit'])
        session.execute(insert(ProviderQuotaPolicy).values(key=row['key'], **values).on_conflict_do_update(
            index_elements=[ProviderQuotaPolicy.key], set_=values))
        policy = session.scalar(select(ProviderQuotaPolicy).where(ProviderQuotaPolicy.key == row['key']).execution_options(populate_existing=True))
        for name in ('blocked_until', 'cooldown_until'):
            if row[name] is not None:
                restored = _aware(row[name])
                setattr(policy, name, max(restored, getattr(policy, name) or restored))
        session.flush()
    for row in payload['reservations']:
        values = {**row, 'reserved_at': _aware(row['reserved_at'])}
        existing = session.get(ProviderQuotaReservation, row['id'])
        if existing is not None and (existing.key != row['key'] or existing.reserved_at != values['reserved_at']):
            raise ValueError('portable quota reservation identity collision')
        session.execute(insert(ProviderQuotaReservation).values(**values).on_conflict_do_nothing(index_elements=[ProviderQuotaReservation.id]))
    for row in payload['rate_budgets']:
        budget = _lock_budget(session, row['key'])
        with _suppress_charge(session):
            session.execute(update(RateBudget).where(RateBudget.key == row['key']).values(
                next_available_at=func.greatest(RateBudget.next_available_at, _aware(row['next_available_at']))))
        session.expire(budget)
    # Allocation rows in a prior checkpoint are audit history, never fresh credit.
    for row in payload['policies']:
        budget = _lock_budget(session, row['key'])
        policy = session.get(ProviderQuotaPolicy, row['key'])
        if policy.require_allocation:
            session.query(ProviderQuotaAllocationDay).filter(ProviderQuotaAllocationDay.key == row['key']).delete(synchronize_session=False)
        _reconcile_gate(session, budget, policy)
    return len(payload['rate_budgets'])
