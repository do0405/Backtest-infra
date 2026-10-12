"""Whole-calendar-day KRX source capture, separate from qualified coverage."""
from collections import deque
from datetime import date, timedelta
import math
import time
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from ..credentials import load_credentials
from ..db import new_id, transaction, utcnow
from ..ingestion.http import ProviderError, ProviderHTTP
from ..ingestion.jobs import (acquire_job, complete_current_job as complete_job, heartbeat_current_job as heartbeat_job,
    LeaseLost, logical_request, stable_hash)
from ..ingestion.models import CollectionJob, TargetUniverseManifest
from .krx_quote_storage import KRX_SERVICES, store_krx_quotes


_LEASE_TTL = timedelta(minutes=5)
_SEOUL = ZoneInfo("Asia/Seoul")


def _services(services):
    if services is None:
        return KRX_SERVICES
    if isinstance(services, (str, bytes)):
        raise ValueError("KRX services must be a nonempty collection")
    values = tuple(services)
    if not values or any(value not in KRX_SERVICES for value in values):
        raise ValueError("Unknown or empty KRX service selection")
    return tuple(service for service in KRX_SERVICES if service in values)


def plan_krx_quotes(session, manifest, start: date, end: date, services=KRX_SERVICES, *, refresh=False, refresh_anchor=None):
    """Plan every requested calendar day, reusing work across target manifests.

    A current security population is never used to discard historical rows or
    market requests. No official trading calendar has been qualified, so weekends
    and holidays remain explicit API requests. Existing jobs are never requeued.
    """
    selected = _services(services)
    if type(start) is not date or type(end) is not date or start > end:
        raise ValueError("Valid inclusive KRX date range required")
    if not isinstance(manifest, TargetUniverseManifest) or not manifest.id:
        raise ValueError("Persisted target manifest required")
    try:
        target_start = date.fromisoformat(manifest.scope["output_start"])
        target_end = date.fromisoformat(manifest.scope["output_end"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Target manifest requires an explicit date range") from None
    if "KR" not in manifest.scope.get("markets", []):
        raise ValueError("Target manifest does not include KR")
    if type(refresh) is not bool:raise ValueError('Explicit KRX refresh flag required')
    if start < target_start or not refresh and end > target_end:
        raise ValueError("KRX dates are outside the target manifest")
    today=utcnow().astimezone(_SEOUL).date()
    if end > today - timedelta(days=1):
        raise ValueError("KRX requires completed Korean calendar days; current or future local days are unclosed")
    if refresh:
        refresh_anchor=refresh_anchor or today
        if type(refresh_anchor) is not date or refresh_anchor>today or end>=refresh_anchor:
            raise ValueError('KRX refresh requires completed dates before its explicit local-day anchor')
    elif refresh_anchor is not None:raise ValueError('KRX refresh anchor requires explicit refresh mode')
    existing = set(session.scalars(select(CollectionJob.identity_hash).where(
        CollectionJob.source == "krx", CollectionJob.domain == "market_quotes")).yield_per(2000))
    pending, created = [], 0
    def flush():
        nonlocal created
        if pending:
            created += len(session.execute(insert(CollectionJob).values(pending)
                .on_conflict_do_nothing(index_elements=[CollectionJob.identity_hash]).returning(CollectionJob.id)).all())
            pending.clear()
    calendar_days = (end - start).days + 1
    for offset in range(calendar_days):
        session_date = (start + timedelta(days=offset)).isoformat()
        for service in selected:
            entity = f"{service}:{session_date}"
            request = {"market": "KR", "service": service, "session_date": session_date, "manifest_id": manifest.id}
            if refresh:
                request.update(original_output_start=target_start.isoformat(),original_output_end=target_end.isoformat(),
                    refresh_anchor=refresh_anchor.isoformat())
            identity = stable_hash(["krx", "market_quotes", entity, logical_request(request)])
            if identity in existing:
                continue
            existing.add(identity)
            pending.append({"id": new_id(), "identity_hash": identity, "source": "krx", "domain": "market_quotes",
                "entity_id": entity, "request": request, "priority": 0 if refresh else 10 + (start - target_start).days + offset})
            if len(pending) == 500:
                flush()
    flush()
    session.flush()
    result={"created_jobs": created, "reused_jobs": calendar_days * len(selected) - created,
        "calendar_days": calendar_days, "planned_requests": calendar_days * len(selected), "services": list(selected),
        "manifest_id": manifest.id, "start": start.isoformat(), "end": end.isoformat(),
        "calendar_policy": "every_calendar_day_until_official_calendar_qualified", "target_scope_preserved": True,
        "historical_population_filtered": False, "coverage_complete": False, "official_calendar_verified": False}
    if refresh:result.update(refresh=True,refresh_anchor=refresh_anchor.isoformat(),original_output_start=target_start.isoformat(),original_output_end=target_end.isoformat())
    return result


def _queue_counts(session, services):
    return dict(session.execute(select(CollectionJob.status, func.count()).where(
        CollectionJob.source == "krx", CollectionJob.domain == "market_quotes",
        CollectionJob.request["market"].astext == "KR", CollectionJob.request["service"].astext.in_(services))
        .group_by(CollectionJob.status)).all())


def run_krx_quote_collection(engine, raw_root, *, budget_seconds, services=None, deadline=None):
    """Resume only KRX market-quote jobs under their existing fenced leases.

    The budget limits this invocation, never the planned dates or population.
    Provider failures stop the source and leave unclaimed jobs pending; lost
    leases are left to their current owner or normal expiry, without requeueing.
    """
    selected = _services(services)
    if not isinstance(budget_seconds, (int, float)) or not math.isfinite(budget_seconds) or budget_seconds <= 0:
        raise ValueError("Positive finite execution budget required")
    if deadline is not None and not math.isfinite(deadline):raise ValueError('Finite execution deadline required')
    start=time.monotonic();local_deadline=start+budget_seconds
    deadline=min(local_deadline,deadline) if deadline is not None else local_deadline
    credentials = load_credentials(sources=["krx"])
    if not getattr(credentials, "krx_api_key", None):
        return {"status": "blocked", "processed_this_run": 0, "blocked_sources": {"krx": "requires_credentials"},
            "requested_services": list(selected), "target_scope_preserved": True, "coverage_complete": False,
            "historical_identity_verified": False, "official_calendar_verified": False, "budget_exhausted": False,
            "outcomes": [], "outcomes_truncated": False}
    # Provider activation and exact transport allowlisting belong to the provider layer.
    from ..providers import KRXData
    http = ProviderHTTP(engine)
    provider = KRXData(http, credentials)
    owner = new_id()
    processed, blocked, outcomes = 0, {}, deque(maxlen=100)
    try:
        while time.monotonic() < deadline:
            with transaction(engine) as session:
                lease = acquire_job(session, owner, utcnow(), _LEASE_TTL, sources=["krx"],
                    domains=["market_quotes"], market="kr", request_services=selected, acquisition_clock=utcnow)
                if lease is None:
                    break
                job = session.get(CollectionJob, lease.job_id)
                request, checkpoint = dict(job.request), dict(job.checkpoint)
            try:
                if time.monotonic()>=deadline:
                    # Only this still-valid fenced claim may be deferred. Keep
                    # its exact checkpoint; a lost lease remains with its owner.
                    with transaction(engine) as session:
                        complete_job(session,lease,utcnow(),checkpoint,status='retry')
                    outcomes.append({'job_id':lease.job_id,'status':'budget_deferred'})
                    break
                service = request["service"]
                session_date = date.fromisoformat(request["session_date"])
                if service not in selected or session_date > utcnow().astimezone(_SEOUL).date() - timedelta(days=1):
                    raise ValueError("Invalid KRX job request")
                response, payload = provider.daily_quotes(service, session_date)
                with transaction(engine) as session:
                    fence = lambda: heartbeat_job(session, lease, utcnow(), _LEASE_TTL)
                    result = store_krx_quotes(session, service, session_date, response, payload, raw_root, fence=fence)
                    result.pop("inserted_ids", None)
                    outcome = {**checkpoint, **result, "coverage_complete": False}
                    status = "succeeded" if result["response_valid"] else "unsupported"
                    if not result["response_valid"]:
                        outcome["reason"] = "invalid_source_envelope"
                    complete_job(session, lease, utcnow(), outcome, status=status)
                outcomes.append({"job_id": lease.job_id, "source": "krx", "domain": "market_quotes", "status": status, **outcome})
                processed += 1
                if not result["response_valid"]:
                    blocked["krx"] = "invalid_source_envelope"
                    break
            except LeaseLost:
                blocked["krx"] = "lease_lost"
                outcomes.append({"job_id": lease.job_id, "status": "lease_lost", "reason": "lease_lost"})
                processed += 1
                break
            except ProviderError as exc:
                reason = exc.code[:300]
                try:
                    with transaction(engine) as session:
                        complete_job(session, lease, utcnow(), {**checkpoint, "reason": reason, "coverage_complete": False}, status="retry")
                except LeaseLost:
                    reason = "lease_lost"
                blocked["krx"] = reason
                outcomes.append({"job_id": lease.job_id, "status": "source_error" if reason != "lease_lost" else "lease_lost", "reason": reason})
                processed += 1
                break
            except (KeyError, TypeError, ValueError):
                reason = "invalid_job_request_or_source_value"
                try:
                    with transaction(engine) as session:
                        complete_job(session, lease, utcnow(), {**checkpoint, "reason": reason, "coverage_complete": False}, status="unsupported")
                except LeaseLost:
                    reason = "lease_lost"
                blocked["krx"] = reason
                outcomes.append({"job_id": lease.job_id, "status": "unsupported" if reason != "lease_lost" else "lease_lost", "reason": reason})
                processed += 1
                break
    finally:
        http.close()
    with transaction(engine) as session:
        counts = _queue_counts(session, selected)
    outstanding = sum(counts.get(status, 0) for status in ("pending", "retry", "running"))
    elapsed = time.monotonic() - start
    return {"status": "blocked" if blocked and not processed else "partial", "processed_this_run": processed,
        "blocked_sources": blocked, "requested_services": list(selected), "queue_status_counts": counts,
        "pending_requests": outstanding, "request_queue_complete": not outstanding and not blocked and not any(counts.get(status, 0) for status in ("unsupported", "failed")),
        "elapsed_seconds": round(elapsed, 2), "budget_seconds": budget_seconds, "budget_exhausted": time.monotonic() >= deadline and outstanding > 0,
        "budget_semantics": "execution_only_never_changes_planned_period_or_population", "target_scope_preserved": True,
        "coverage_complete": False, "historical_identity_verified": False, "official_calendar_verified": False,
        "units_verified": False, "canonical_price_eligible": False, "outcomes": list(outcomes),
        "outcomes_truncated": processed > len(outcomes)}
