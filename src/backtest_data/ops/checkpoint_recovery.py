"""Finite detached continuation for declared lease loss and DB transport loss.

This layer owns waiting and subprocess attempt policy. It never edits leases,
work checkpoints, supplier state or data. The collector retains fencing and
decides whether any new attempt can acquire an expired job.
"""
import math
import time

from .db_recovery import AttemptResult, DEFAULT_RETRY_DELAYS, retryable_result

LEASE_RETRY_DELAYS = (60, 120, 300, 600, 1200, 1800)


def lease_retryable_result(result):
    receipt = result.receipt
    if result.exit_code != 0 or not isinstance(receipt, dict) or receipt.get("status") != "partial":
        return False
    sources = receipt.get("blocked_sources")
    stages = receipt.get("blocked_stages", [])
    if not isinstance(sources, dict) or not isinstance(stages, list):
        return False
    return bool(sources or stages) and all(reason == "lease_lost" for reason in sources.values()) and all(
        isinstance(stage, dict) and stage.get("reason") == "lease_lost" for stage in stages)


def run_with_checkpoint_recovery(execute, *, database_retry_delays=DEFAULT_RETRY_DELAYS,
        lease_retry_delays=LEASE_RETRY_DELAYS, total_budget_seconds=None,
        sleep=time.sleep, monotonic=time.monotonic, record=lambda event: None):
    database_delays, lease_delays = tuple(database_retry_delays), tuple(lease_retry_delays)
    for delays in (database_delays, lease_delays):
        if len(delays) > 6 or any(type(delay) not in (int, float) or not math.isfinite(delay) or delay <= 0 for delay in delays):
            raise ValueError("bounded_positive_checkpoint_retry_delays_required")
    if total_budget_seconds is not None and (type(total_budget_seconds) not in (int, float)
            or not math.isfinite(total_budget_seconds) or total_budget_seconds <= 0):
        raise ValueError("positive_worker_budget_required")
    deadline = monotonic() + total_budget_seconds if total_budget_seconds is not None else None
    database_retries = lease_retries = 0

    def remaining():
        return max(0., deadline - monotonic()) if deadline is not None else None

    def deferred():
        receipt = {"status": "budget_deferred", "reason": "checkpoint_recovery_budget_exhausted",
            "coverage_complete": False}
        record(receipt)
        return AttemptResult(0, receipt)

    for attempt in range(1, len(database_delays) + len(lease_delays) + 2):
        budget = remaining()
        if budget is not None and budget <= 0:
            return deferred()
        result = execute(attempt, budget)
        if not isinstance(result, AttemptResult):
            raise TypeError("worker_attempt_result_required")
        if retryable_result(result):
            if database_retries == len(database_delays):
                record({"status": "database_retry_exhausted", "attempt": attempt,
                    "reason": "database_retry_limit_reached"})
                return result
            delay = database_delays[database_retries]
            database_retries += 1
            event = {"status": "waiting_for_database", "attempt": attempt}
        elif lease_retryable_result(result):
            if lease_retries == len(lease_delays):
                record({"status": "lease_retry_exhausted", "attempt": attempt,
                    "reason": "lease_retry_limit_reached"})
                return result
            delay = lease_delays[lease_retries]
            lease_retries += 1
            event = {"status": "waiting_for_lease_resume", "attempt": attempt,
                "lease_retry": lease_retries, "reason": "lease_lost"}
        else:
            return result
        budget = remaining()
        if budget is not None and budget <= 0:
            return deferred()
        event["retry_in_seconds"] = min(delay, budget) if budget is not None else delay
        record(event)
        sleep(event["retry_in_seconds"])
    raise AssertionError("unreachable_checkpoint_recovery_state")
