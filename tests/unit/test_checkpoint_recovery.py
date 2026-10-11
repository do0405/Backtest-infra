"""Detached continuation must be selective, finite and preserve its budget."""
from backtest_data.ops.db_recovery import AttemptResult


def lease_loss(**changes):
    return {"status": "partial", "blocked_sources": {}, "blocked_stages": [
        {"source": "alpaca", "stage": "prices", "reason": "lease_lost"}],
        "coverage_complete": False, **changes}


def runner():
    from backtest_data.ops.checkpoint_recovery import run_with_checkpoint_recovery
    return run_with_checkpoint_recovery


def test_lease_loss_continues_after_delay_with_same_executor_and_smaller_budget():
    clock = [0.0]
    calls, events = [], []

    def execute(attempt, remaining):
        calls.append((attempt, remaining))
        clock[0] += 5
        return AttemptResult(0, lease_loss() if attempt == 1 else {"status": "complete"})

    result = runner()(execute, lease_retry_delays=(60,), total_budget_seconds=100,
        monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0]+seconds), record=events.append)
    assert result.receipt["status"] == "complete"
    assert calls == [(1, 100), (2, 35)]
    assert events[0] == {"status": "waiting_for_lease_resume", "attempt": 1,
        "retry_in_seconds": 60, "lease_retry": 1, "reason": "lease_lost"}


def test_repeated_lease_loss_stops_after_declared_retry_limit():
    calls, events = [], []

    def execute(attempt, remaining):
        calls.append(attempt)
        return AttemptResult(0, lease_loss())

    result = runner()(execute, lease_retry_delays=(1, 2), sleep=lambda seconds: None, record=events.append)
    assert calls == [1, 2, 3]
    assert result.receipt["status"] == "partial"
    assert events[-1]["status"] == "lease_retry_exhausted"


def test_provider_block_unknown_partial_and_failure_never_restart():
    for result in [AttemptResult(2, {"status": "blocked", "reason": "401"}),
        AttemptResult(0, lease_loss(blocked_sources={"opendart": "800"})),
        AttemptResult(0, {"status": "partial"}),
        AttemptResult(0, lease_loss(blocked_stages=[{"reason": "quota"}])),
        AttemptResult(3, lease_loss()), AttemptResult(0, None)]:
        calls = []
        answer = runner()(lambda attempt, budget: (calls.append(attempt) or result),
            lease_retry_delays=(1,), sleep=lambda seconds: None)
        assert calls == [1] and answer == result


def test_database_and_lease_retries_share_deadline_and_separate_finite_limits():
    clock, calls = [0.0], []
    db_loss = AttemptResult(2, {"status": "blocked", "reason": "OperationalError", "database_error": {
        "category": "server_recovery", "sqlstate": "57P03", "retryable": True}})

    def execute(attempt, remaining):
        calls.append((attempt, remaining))
        return db_loss if attempt in (1, 3) else AttemptResult(0, lease_loss())

    result = runner()(execute, database_retry_delays=(10,), lease_retry_delays=(20,),
        total_budget_seconds=100, monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    assert calls == [(1, 100), (2, 90), (3, 70)]
    assert result == db_loss  # DB allowance is not reset after a lease attempt.


def test_expired_shared_budget_never_starts_another_child():
    clock, calls = [0.0], []

    def execute(attempt, remaining):
        calls.append(attempt)
        clock[0] = 99
        return AttemptResult(0, lease_loss())

    result = runner()(execute, lease_retry_delays=(60,), total_budget_seconds=100,
        monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    assert calls == [1]
    assert result.receipt == {"status": "budget_deferred", "reason": "checkpoint_recovery_budget_exhausted",
        "coverage_complete": False}


def test_krx_source_lease_loss_is_retryable_but_mixed_provider_errors_are_not():
    calls = []
    receipts = [AttemptResult(0, {"status": "partial", "blocked_sources": {"krx": "lease_lost"}}),
        AttemptResult(0, {"status": "partial", "blocked_sources": {"krx": "401"}})]
    answer = runner()(lambda attempt, budget: (calls.append(attempt) or receipts[attempt-1]),
        lease_retry_delays=(1,), sleep=lambda seconds: None)
    assert calls == [1, 2] and answer == receipts[1]
