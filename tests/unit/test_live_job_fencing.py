"""Exercise legacy and live ownership checks with deterministic lock/IO waits."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from backtest_data.ingestion import jobs


NOW = datetime(2024, 7, 10, 18, tzinfo=timezone.utc)
TTL = timedelta(minutes=5)
LEASE = jobs.JobLease("job", "owner", 7)


class WaitingSession:
    def __init__(self, *, wait_seconds=0, update_seconds=0, expired=False, superseded=False, remaining_seconds=None):
        self.now = NOW
        self.deadline = NOW + (timedelta(seconds=-1) if expired else TTL)
        if remaining_seconds is not None:
            self.deadline = NOW + timedelta(seconds=remaining_seconds)
        self.owner, self.token, self.status = "owner", (8 if superseded else 7), "running"
        self.checkpoint = {"cursor": "original"}
        self.wait_seconds, self.update_seconds = wait_seconds, update_seconds
        self.pending, self.flushed = [], []
        self.suppressed = 0
        self.statements = []

    @property
    @contextmanager
    def no_autoflush(self):
        self.suppressed += 1
        try:
            yield
        finally:
            self.suppressed -= 1

    def _autoflush(self):
        if not self.suppressed:
            self.flushed.extend(self.pending)
            self.pending.clear()

    @contextmanager
    def page_transaction(self):
        # Existing collectors propagate LeaseLost out of the whole page.
        snapshot = (self.deadline, self.owner, self.token, self.status, dict(self.checkpoint))
        try:
            yield
        except Exception:
            self.deadline, self.owner, self.token, self.status, self.checkpoint = snapshot
            raise

    def scalar(self, statement):
        self._autoflush()
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        self.statements.append(sql)
        if "FOR UPDATE" in sql:
            # SELECT must qualify ownership, without evaluating expiry before
            # the wait. The row remains unchanged throughout the wait.
            assert "lease_until >" not in sql
            self.now += timedelta(seconds=self.wait_seconds)
            self.wait_seconds = 0
            return self.deadline if self.owner == LEASE.owner and self.token == LEASE.fencing_token and self.status == "running" else None
        assert "clock_timestamp()" in sql
        return self.now

    def execute(self, statement):
        self._autoflush()
        compiled = statement.compile(dialect=postgresql.dialect())
        sql, params = str(compiled), compiled.params
        self.statements.append(sql)
        # A legacy UPDATE can evaluate its explicit timestamp before waiting
        # for an unchanged row lock; no tuple update triggers a recheck.
        checked_at = self.now if "clock_timestamp()" in sql else params["lease_until_1"]
        self.now += timedelta(seconds=self.wait_seconds)
        self.wait_seconds = 0
        valid = self.deadline > checked_at and self.owner == LEASE.owner and self.token == LEASE.fencing_token and self.status == "running"
        if valid:
            if "checkpoint" in params:
                self.checkpoint, self.status = params["checkpoint"], params["status"]
                self.owner = self.deadline = None
            else:
                self.deadline = (self.now + params["clock_timestamp_1"]) if "clock_timestamp()" in sql else params["lease_until"]
            self.now += timedelta(seconds=self.update_seconds)
        result = SimpleNamespace(checked_at=self.now, lease_until=self.deadline)
        return SimpleNamespace(rowcount=int(valid), one_or_none=lambda: result if valid else None)


def live_operation(session, operation):
    if operation == "heartbeat":
        return getattr(jobs, "heartbeat_current_job", jobs.heartbeat_job)(session, LEASE, NOW, TTL)
    return getattr(jobs, "complete_current_job", jobs.complete_job)(session, LEASE, NOW, {"cursor": "changed"})


@pytest.mark.parametrize("operation", ["heartbeat", "complete"])
def test_live_lease_rejects_unchanged_row_after_lock_wait_crosses_expiry(operation):
    # The previous heartbeat's remaining window is shorter than a new renewal.
    session = WaitingSession(wait_seconds=31, remaining_seconds=30)
    original_deadline = session.deadline
    with pytest.raises(jobs.LeaseLost, match="Expired or superseded lease"):
        live_operation(session, operation)
    assert session.deadline == original_deadline and session.status == "running"
    assert session.checkpoint == {"cursor": "original"} and session.owner == "owner"


@pytest.mark.parametrize("operation", ["heartbeat", "complete"])
def test_live_lease_checks_owner_before_autoflush(operation):
    session = WaitingSession(expired=True)
    evidence = object()
    session.pending.append(evidence)
    with pytest.raises(jobs.LeaseLost):
        live_operation(session, operation)
    assert session.pending == [evidence] and session.flushed == []


@pytest.mark.parametrize("operation", ["heartbeat", "complete"])
def test_live_lease_rejects_superseded_token(operation):
    session = WaitingSession(superseded=True)
    with pytest.raises(jobs.LeaseLost):
        live_operation(session, operation)
    assert session.deadline == NOW + TTL and session.checkpoint == {"cursor": "original"}


@pytest.mark.parametrize("operation", ["heartbeat", "complete"])
def test_live_update_io_expiry_rolls_back_enclosing_page(operation):
    session = WaitingSession(update_seconds=301)
    with pytest.raises(jobs.LeaseLost), session.page_transaction():
        live_operation(session, operation)
    assert session.deadline == NOW + TTL and session.status == "running"
    assert session.checkpoint == {"cursor": "original"} and session.owner == "owner"


def test_live_heartbeat_renews_from_fresh_clock_after_short_lock_wait():
    session = WaitingSession(wait_seconds=290)
    live_operation(session, "heartbeat")
    assert session.deadline == session.now + TTL
    assert "FOR UPDATE" in session.statements[0]
    assert any("clock_timestamp()" in sql and sql.startswith("UPDATE") for sql in session.statements)


def test_explicit_time_heartbeat_and_completion_remain_deterministic():
    session = WaitingSession(wait_seconds=301)
    jobs.heartbeat_job(session, LEASE, NOW, TTL)
    assert session.deadline == NOW + TTL
    jobs.complete_job(session, LEASE, NOW, {"cursor": "explicit"})
    assert session.status == "succeeded" and session.checkpoint == {"cursor": "explicit"}
    assert all("clock_timestamp()" not in sql and "FOR UPDATE" not in sql for sql in session.statements)


def test_core_and_krx_collectors_import_the_live_lease_helpers():
    from backtest_data.ingestion import supervisor
    from backtest_data.prices import krx_quote_collection
    for module in (supervisor, krx_quote_collection):
        assert module.heartbeat_job.__name__ == "heartbeat_current_job"
        assert module.complete_job.__name__ == "complete_current_job"
    assert krx_quote_collection._LEASE_TTL == TTL
