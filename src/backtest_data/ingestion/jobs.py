from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json

from sqlalchemy import func, or_, select, update, text
from sqlalchemy.dialects.postgresql import insert

from ..db import new_id, utcnow
from .models import CollectionJob, RateBudget


def stable_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def logical_request(request):
    # The target manifest binds coverage, while source request semantics bind work.
    return {key: value for key, value in request.items() if key != "manifest_id"}


def plan_job(session, *, source: str, domain: str, entity_id: str, request: dict, priority: int = 10):
    specification = logical_request(request)
    existing = session.scalar(select(CollectionJob).where(CollectionJob.source == source,
        CollectionJob.domain == domain, CollectionJob.entity_id == entity_id,
        CollectionJob.request.op("-")("manifest_id") == specification,
        CollectionJob.status != "superseded").limit(1))
    if existing:
        return existing
    identity = stable_hash([source, domain, entity_id, specification])
    statement = insert(CollectionJob).values(id=new_id(), identity_hash=identity, source=source, domain=domain,
        entity_id=entity_id, request=request, priority=priority).on_conflict_do_nothing(index_elements=[CollectionJob.identity_hash])
    session.execute(statement)
    return session.scalar(select(CollectionJob).where(CollectionJob.identity_hash == identity))


def consolidate_pending_jobs(session, *, sources=None):
    """Retain every work/evidence row while consolidating redundant pending work."""
    result = session.execute(text("""
        WITH ranked AS (
          SELECT id, status, first_value(id) OVER w AS canonical_id, row_number() OVER w AS position
          FROM collection_job
          WHERE (:filter_sources = false OR source = ANY(CAST(:sources AS text[])))
          WINDOW w AS (PARTITION BY source, domain, entity_id, request - 'manifest_id'
            ORDER BY CASE status WHEN 'succeeded' THEN 0 WHEN 'running' THEN 1 WHEN 'retry' THEN 2
              WHEN 'unsupported' THEN 3 WHEN 'failed' THEN 4 WHEN 'pending' THEN 5 ELSE 6 END,
              attempts DESC, id)
        )
        UPDATE collection_job j SET status='superseded',
          checkpoint=j.checkpoint || jsonb_build_object('canonical_job_id', r.canonical_id, 'reason', 'identical_request_reused')
        FROM ranked r WHERE j.id=r.id AND r.position>1 AND j.status='pending'
    """), {"filter_sources": sources is not None, "sources": list(sources or [])})
    return result.rowcount


def request_repair(session, *, domains, market, job_ids=None, sources=None):
    """Requery original periods, optionally limited to explicit terminal jobs."""
    statement = select(CollectionJob).where(CollectionJob.domain.in_(domains),
        or_(CollectionJob.status.in_(["unsupported", "failed"]),
            (CollectionJob.status == "succeeded") & (CollectionJob.domain == "filing_sql") &
            (CollectionJob.checkpoint["source_format_complete"].as_boolean() == False))).with_for_update(skip_locked=True)
    if market != "both":
        statement = statement.where(CollectionJob.request["market"].astext == market.upper())
    if job_ids is not None:
        statement = statement.where(CollectionJob.id.in_(job_ids))
    if sources is not None:
        statement = statement.where(CollectionJob.source.in_(sources))
    repaired = 0
    for job in session.scalars(statement):
        if job.source == 'kis_overseas' and job.request.get('mapping_status') == 'unsupported':
            continue
        prior = {key: value for key, value in job.checkpoint.items() if key not in {"prior_traversal"}}
        job.checkpoint = {**job.checkpoint, "repair_requested_at": utcnow().isoformat(),
                          "previous_terminal_status": job.status,
                          "repair_generation": int(job.checkpoint.get("repair_generation", 0)) + 1}
        if job.domain == "prices":
            job.checkpoint = {**job.checkpoint, "prior_traversal": prior, "rejected_total": 0, "source_rows_total": 0}
            job.checkpoint.pop("cursor_end", None)
            job.checkpoint.pop("next_page_token", None)
            job.checkpoint.pop("request_anchor", None)
        job.status = "retry"
        repaired += 1
    session.flush()
    return repaired


@dataclass(frozen=True)
class JobLease:
    job_id: str
    owner: str
    fencing_token: int


class LeaseLost(ValueError):
    """This owner can no longer write the job's terminal state or checkpoint."""


def acquire_job(session, owner: str, now: datetime, ttl: timedelta, *, domains=None, market=None,
                sources=None, fair=False, request_services=None, acquisition_clock=None) -> JobLease | None:
    if ttl.total_seconds() <= 0:
        raise ValueError("Positive lease TTL required")
    statement = select(CollectionJob).where(
        # Literal guard lets PostgreSQL prove the partial index predicate even
        # with the parameterized expired-running branch below.
        text("collection_job.status IN ('pending','retry','running')"),
        or_(CollectionJob.status.in_(["pending", "retry"]),
            (CollectionJob.status == "running") & (CollectionJob.lease_until <= now))
        )
    if domains:
        statement = statement.where(CollectionJob.domain.in_(domains))
    if sources is not None:
        statement = statement.where(CollectionJob.source.in_(sources))
    if request_services is not None:
        statement = statement.where(CollectionJob.request["service"].astext.in_(request_services))
    if market and market != "both":
        statement = statement.where(CollectionJob.request["market"].astext == market.upper())
    ordering = ([CollectionJob.attempts] if fair else []) + [CollectionJob.priority, CollectionJob.id]
    job = session.scalar(statement.order_by(*ordering).with_for_update(skip_locked=True).limit(1))
    if job is None:
        return None
    # A slow scan must not spend the new owner's lease before the row is locked.
    # Explicit-time callers retain their existing deterministic contract.
    acquired_at = now
    if acquisition_clock is not None:
        clock_value = acquisition_clock()
        if not isinstance(clock_value, datetime) or clock_value.tzinfo is None or clock_value.utcoffset() is None:
            raise ValueError("Timezone-aware acquisition clock required")
        acquired_at = max(now, clock_value)
    job.status, job.lease_owner, job.lease_until = "running", owner, acquired_at + ttl
    job.fencing_token += 1
    job.attempts += 1
    session.flush()
    return JobLease(job.id, owner, job.fencing_token)


def heartbeat_job(session, lease: JobLease, now: datetime, ttl: timedelta):
    result = session.execute(update(CollectionJob).where(CollectionJob.id == lease.job_id,
        CollectionJob.lease_owner == lease.owner, CollectionJob.fencing_token == lease.fencing_token,
        CollectionJob.lease_until > now, CollectionJob.status == "running").values(lease_until=now + ttl))
    if result.rowcount != 1:
        raise LeaseLost("Expired or superseded lease")


def complete_job(session, lease: JobLease, now: datetime, checkpoint: dict, *, status="succeeded"):
    if status not in {"succeeded", "retry", "unsupported", "failed"}:
        raise ValueError("Invalid job terminal state")
    result = session.execute(update(CollectionJob).where(CollectionJob.id == lease.job_id,
        CollectionJob.lease_owner == lease.owner, CollectionJob.fencing_token == lease.fencing_token,
        CollectionJob.lease_until > now, CollectionJob.status == "running")
        .values(status=status, checkpoint=checkpoint, lease_owner=None, lease_until=None))
    if result.rowcount != 1:
        raise LeaseLost("Expired or superseded lease")


def _write_current_job(session, lease, values, *, renewal):
    """Check server time after owning the row; let expiry roll back the page."""
    ownership = (CollectionJob.id == lease.job_id, CollectionJob.lease_owner == lease.owner,
        CollectionJob.fencing_token == lease.fencing_token, CollectionJob.status == "running")
    with session.no_autoflush:
        # A time predicate here could be evaluated before an unchanged-row
        # lock wait. Lock by ownership first, then evaluate time in the UPDATE.
        deadline = session.scalar(select(CollectionJob.lease_until).where(*ownership).with_for_update())
        if deadline is None:
            raise LeaseLost("Expired or superseded lease")
        written = session.execute(update(CollectionJob).where(*ownership,
            CollectionJob.lease_until > func.clock_timestamp()).values(**values)
            .execution_options(synchronize_session=False)
            .returning(CollectionJob.lease_until, func.clock_timestamp().label("checked_at"))).one_or_none()
        if written is None:
            raise LeaseLost("Expired or superseded lease")
        # Include driver/IO delay after RETURNING. No helper savepoint: current
        # collectors propagate LeaseLost out of their whole page transaction.
        current = session.scalar(select(func.clock_timestamp()))
        expires_at = written.lease_until if renewal else deadline
        if current >= expires_at:
            raise LeaseLost("Expired or superseded lease")


def heartbeat_current_job(session, lease: JobLease, now: datetime, ttl: timedelta):
    """Opt into server wall time; retain the explicit helper's call signature."""
    if ttl.total_seconds() <= 0:
        raise ValueError("Positive lease TTL required")
    _write_current_job(session, lease, {"lease_until": func.clock_timestamp() + ttl}, renewal=True)


def complete_current_job(session, lease: JobLease, now: datetime, checkpoint: dict, *, status="succeeded"):
    """Complete only with current ownership; explicit-time callers stay separate."""
    if status not in {"succeeded", "retry", "unsupported", "failed"}:
        raise ValueError("Invalid job terminal state")
    _write_current_job(session, lease, {"status": status, "checkpoint": checkpoint,
        "lease_owner": None, "lease_until": None}, renewal=False)


class RateReservationDeferred(RuntimeError):
    pass


def reserve_rate_slot(session, key: str, now: datetime, minimum_interval: float, *, max_delay_seconds=None) -> datetime:
    if minimum_interval <= 0:
        raise ValueError("Positive source interval required")
    session.execute(insert(RateBudget).values(key=key, next_available_at=now)
                    .on_conflict_do_nothing(index_elements=[RateBudget.key]))
    budget = session.scalar(select(RateBudget).where(RateBudget.key == key).with_for_update())
    available = max(now, budget.next_available_at)
    if max_delay_seconds is not None and (available - now).total_seconds() > max_delay_seconds:
        raise RateReservationDeferred("Source budget deferred before reservation")
    budget.next_available_at = available + timedelta(seconds=minimum_interval)
    session.flush()
    return available
