"""Additive, bounded DDL applying admission to legacy rate_budget UPDATEs."""
from sqlalchemy import text

from .quota_models import ProviderQuotaAllocationDay, ProviderQuotaPolicy, ProviderQuotaReservation


QUOTA_TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION backtest_provider_quota_admit() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    policy provider_quota_policy%ROWTYPE;
    slot timestamptz;
    wall_clock timestamptz;
    day_start timestamptz;
    reset_at timestamptz;
    used bigint;
    allowance integer;
BEGIN
    IF current_setting('backtest.quota_suppress', true) = 'on' THEN
        RETURN NEW;
    END IF;
    SELECT * INTO policy FROM provider_quota_policy WHERE key = NEW.key FOR UPDATE;
    IF NOT FOUND THEN RETURN NEW; END IF;
    wall_clock := clock_timestamp();
    slot := GREATEST(OLD.next_available_at, wall_clock);
    NEW.next_available_at := GREATEST(NEW.next_available_at,
        GREATEST(OLD.next_available_at, wall_clock) + policy.minimum_interval_seconds * interval '1 second');
    IF policy.cooldown_until IS NOT NULL THEN
        NEW.next_available_at := GREATEST(NEW.next_available_at, policy.cooldown_until);
    END IF;
    IF policy.window_kind = 'none' THEN RETURN NEW; END IF;
    allowance := policy.credit_limit;
    IF policy.window_kind = 'calendar_day' THEN
        day_start := (slot AT TIME ZONE policy.timezone)::date::timestamp AT TIME ZONE policy.timezone;
        reset_at := ((slot AT TIME ZONE policy.timezone)::date + 1)::timestamp AT TIME ZONE policy.timezone;
        IF policy.require_allocation THEN
            SELECT LEAST(allowance, credit_limit) INTO allowance
              FROM provider_quota_allocation_day
              WHERE key = NEW.key AND allocation_date = (slot AT TIME ZONE policy.timezone)::date;
            allowance := COALESCE(allowance, 0);
        END IF;
        SELECT count(*) INTO used FROM provider_quota_reservation
            WHERE key = NEW.key AND reserved_at >= day_start AND reserved_at < reset_at;
    ELSE
        SELECT count(*), min(reserved_at) + interval '24 hours' INTO used, reset_at
            FROM provider_quota_reservation WHERE key = NEW.key AND reserved_at > slot - interval '24 hours';
    END IF;
    -- Exhaustion is represented by an absolute next clock, not a SQL exception.
    -- New callers preflight allocation gates; existing callers observe the final
    -- allowed reservation arming this clock before the next UPDATE can occur.
    IF used < allowance THEN
        INSERT INTO provider_quota_reservation(key, reserved_at) VALUES (NEW.key, slot);
        used := used + 1;
        IF policy.window_kind = 'rolling_24h' THEN
            SELECT min(reserved_at) + interval '24 hours' INTO reset_at
                FROM provider_quota_reservation WHERE key = NEW.key AND reserved_at > slot - interval '24 hours';
        END IF;
    END IF;
    IF used >= allowance THEN
        NEW.next_available_at := GREATEST(NEW.next_available_at, reset_at);
        UPDATE provider_quota_policy SET blocked_until = reset_at WHERE key = NEW.key;
    END IF;
    DELETE FROM provider_quota_reservation WHERE key = NEW.key AND reserved_at < wall_clock - interval '48 hours';
    RETURN NEW;
END;
$$
"""


def install_quota_schema(connection, *, lock_timeout='2s', statement_timeout='15s'):
    """Install only these additive controls; caller owns the transaction."""
    old = connection.execute(text("SELECT current_setting('lock_timeout'), current_setting('statement_timeout')")).one()
    connection.execute(text("SELECT set_config('lock_timeout', :lock, true), set_config('statement_timeout', :statement, true)"),
        {'lock': lock_timeout, 'statement': statement_timeout})
    for model in (ProviderQuotaPolicy, ProviderQuotaReservation, ProviderQuotaAllocationDay):
        model.__table__.create(connection, checkfirst=True)
    connection.execute(text(QUOTA_TRIGGER_SQL))
    # Avoid replacing an already installed trigger, and avoid INSERT charging:
    # INSERT ... ON CONFLICT DO NOTHING executes BEFORE INSERT triggers too.
    connection.execute(text("""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'rate_budget'::regclass
            AND tgname = 'provider_quota_admission') THEN
            CREATE TRIGGER provider_quota_admission BEFORE UPDATE ON rate_budget
            FOR EACH ROW EXECUTE FUNCTION backtest_provider_quota_admit();
        END IF;
    END $$"""))
    connection.execute(text("SELECT set_config('lock_timeout', :lock, true), set_config('statement_timeout', :statement, true)"),
        {'lock': old[0], 'statement': old[1]})
