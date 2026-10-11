"""One bounded/resumable collector; run budget never changes the universe."""
from datetime import date, datetime, timedelta
import time
import math
from collections import deque

from sqlalchemy import func, select

from ..catalog.models import Security
from ..credentials import load_credentials
from ..db import new_id, transaction, utcnow
from ..fundamentals.collection import discover_issuer, collect_filing
from ..fundamentals.dart_json_collection import collect_dart_json, plan_dart_json
from ..providers import AlpacaData, KISQuotes, OpenDART, SEC
from ..prices.adapters.alpaca import AlpacaDailyRequest
from ..prices.alpaca_storage import store_alpaca_prices
from ..prices.adapters.kis import KISDailyRequest
from ..prices.storage import store_kis_prices
from ..prices.adapters.kis_overseas import KISOverseasDailyRequest
from ..prices.kis_overseas_storage import store_kis_overseas_prices
from .http import ProviderHTTP, ProviderError
from .jobs import (acquire_job, complete_current_job as complete_job, heartbeat_current_job as heartbeat_job,
    consolidate_pending_jobs, request_repair, LeaseLost)
from .models import CollectionJob


def _source_access(credentials, market, domain):
    """Exclude absent transports before claiming work; preserve the full queue."""
    allowed, blocked = [], {}
    if domain == "prices":
        if market in {"kr", "both"}:
            if getattr(credentials, "kis_app_key", None) and getattr(credentials, "kis_app_secret", None):
                allowed.append("kis")
            else:
                blocked["kis"] = "requires_credentials"
        if market in {"us", "both"}:
            if getattr(credentials, "kis_app_key", None) and getattr(credentials, "kis_app_secret", None):
                allowed.append("kis_overseas")
            else:
                blocked["kis_overseas"] = "requires_credentials"
            if getattr(credentials, "alpaca_key", None) and getattr(credentials, "alpaca_secret", None):
                allowed.append("alpaca")
            else:
                blocked["alpaca"] = "requires_credentials_for_free_historical_sip"
    else:
        if market in {"kr", "both"}:
            if getattr(credentials, "opendart_api_key", None):
                allowed.append("opendart")
            else:
                blocked["opendart"] = "requires_credentials"
        if market in {"us", "both"}:
            if "@" in (getattr(credentials, "sec_user_agent", "") or ""):
                allowed.append("sec")
            else:
                blocked["sec"] = "requires_identifying_user_agent"
    return allowed, blocked


def run_collection(engine, raw_root, *, market, domain, budget_seconds=None, mode="incremental", source_filter=None):
    if domain not in {"prices", "fundamentals", "indices"}:
        raise ValueError("Unknown collection domain")
    if domain == "indices":
        return {"status": "blocked", "reason": "qualified_historical_index_source_required", "target_scope_preserved": True}
    if mode not in {"bootstrap", "incremental", "repair"}:
        raise ValueError("Unknown collection mode")
    if budget_seconds is not None and (not math.isfinite(budget_seconds) or budget_seconds <= 0):
        raise ValueError("Positive execution budget required")
    compatible = set()
    if domain == "prices":
        if market in {"kr", "both"}: compatible.add("kis")
        if market in {"us", "both"}: compatible.update({"kis_overseas", "alpaca"})
    else:
        if market in {"kr", "both"}: compatible.add("opendart")
        if market in {"us", "both"}: compatible.add("sec")
    if source_filter is not None:
        if not source_filter or not set(source_filter).issubset(compatible):
            raise ValueError("Requested source is incompatible with market/domain")
        source_filter = sorted(set(source_filter))
    credentials = load_credentials(sources=source_filter if source_filter is not None else sorted(compatible))
    http = ProviderHTTP(engine)
    sources, blocked_sources = _source_access(credentials, market, domain)
    if source_filter is not None:
        sources = [source for source in sources if source in source_filter]
        blocked_sources = {source: reason for source, reason in blocked_sources.items() if source in source_filter}
    blocked_stages = {}
    kis, dart, sec = KISQuotes(http, credentials), OpenDART(http, credentials), SEC(http, credentials)
    alpaca = AlpacaData(http, credentials)
    owner, start, outcomes = new_id(), time.monotonic(), deque(maxlen=100)
    processed = 0
    domains = ["prices"] if domain == "prices" else ["fundamentals", "issuer_discovery", "filing_sql", "filing_json"]
    lane_order = [(source, stage) for stage in domains for source in sources]
    with transaction(engine) as session:
        consolidated = consolidate_pending_jobs(session, sources=source_filter)
        json_planned = plan_dart_json(session)["created_jobs"] if domain == "fundamentals" and "opendart" in sources else 0
        repair_count = request_repair(session, domains=domains, market=market, sources=source_filter) if mode == "repair" else 0
        attempts = select(CollectionJob.source, CollectionJob.domain, func.sum(CollectionJob.attempts)).where(
            CollectionJob.source.in_(sources), CollectionJob.domain.in_(domains), CollectionJob.status != "superseded")
        if market != "both":
            attempts = attempts.where(CollectionJob.request["market"].astext == market.upper())
        durable_attempts = {(row[0], row[1]): row[2] for row in session.execute(
            attempts.group_by(CollectionJob.source, CollectionJob.domain))}
        lanes = deque(sorted(lane_order, key=lambda lane: durable_attempts.get(lane, 0)))
    setup_elapsed = time.monotonic() - start
    start = time.monotonic()

    def record_lease_loss(lease,source,stage):
        nonlocal processed
        # Keep the expired work/checkpoint intact. Its fencing token prevents
        # this owner from completing it; other lanes can still progress. Avoid
        # immediately reclaiming the same slow stage in a tight retry loop.
        blocked_stages[(source,stage)]='lease_lost'
        outcomes.append({'job_id':lease.job_id,'source':source,'domain':stage,
            'status':'lease_lost','reason':'lease_lost'})
        processed+=1
    try:
        while budget_seconds is None or time.monotonic() - start < budget_seconds:
            with transaction(engine) as session:
                lease = None
                for _ in range(len(lanes)):
                    lane_source, lane_stage = lanes.popleft()
                    lanes.append((lane_source, lane_stage))
                    if lane_source in blocked_sources or (lane_source, lane_stage) in blocked_stages:
                        continue
                    lease = acquire_job(session, owner, utcnow(), timedelta(minutes=5), domains=[lane_stage],
                                        sources=[lane_source], market=market, fair=True, acquisition_clock=utcnow)
                    if lease is not None:
                        break
                if lease is None:
                    break
                job = session.get(CollectionJob, lease.job_id)
                source, job_domain, entity_id, request, checkpoint = job.source, job.domain, job.entity_id, dict(job.request), dict(job.checkpoint)
            try:
                status, outcome = "succeeded", {}
                if job_domain == "prices":
                    if source == "alpaca":
                        anchor = datetime.fromisoformat(checkpoint["request_anchor"]) if checkpoint.get("next_page_token") else utcnow()
                        api_request = AlpacaDailyRequest(date.fromisoformat(request["output_start"]), date.fromisoformat(request["output_end"]),
                            symbol=request["symbol"], page_token=checkpoint.get("next_page_token"), captured_at=anchor,
                            page_size=checkpoint.get("page_size",10000) if checkpoint.get("next_page_token") else request.get("page_size",100))
                        response, payload = alpaca.daily_prices(api_request)
                        with transaction(engine) as session:
                            heartbeat_job(session, lease, utcnow(), timedelta(minutes=5))
                            outcome = {**checkpoint, **store_alpaca_prices(session, entity_id, api_request, response, payload, raw_root,
                                fence=lambda: heartbeat_job(session, lease, utcnow(), timedelta(minutes=5)))}
                            outcome["rejected_total"] = checkpoint.get("rejected_total", 0) + outcome["rejected"]
                            outcome["source_rows_total"] = checkpoint.get("source_rows_total", 0) + outcome["source_row_count"]
                            if not outcome["response_valid"] or not outcome["pagination_valid"]:
                                status, outcome["reason"] = "unsupported", "source_response_or_pagination_gap"
                            elif outcome["next_page_token"]:
                                status = "retry"
                            elif outcome["rejected_total"]:
                                status, outcome["reason"] = "unsupported", "source_row_validation_gap"
                            elif not outcome["source_rows_total"]:
                                status, outcome["reason"] = "unsupported", "source_no_data"
                            elif outcome.get("deferred_recent_start"):
                                outcome["reason"] = "api_traversal_complete_recent_interval_deferred"
                            complete_job(session, lease, utcnow(), outcome, status=status)
                        outcomes.append({"job_id": lease.job_id, "source": source, "domain": job_domain, "status": status, **outcome})
                        processed += 1
                        continue
                    elif source == "kis_overseas":
                        proof = request.get('mapping_evidence') or {}
                        if (request.get('mapping_status') == 'unsupported' or
                            not all(request.get(key) for key in ('provider_symbol', 'exchange', 'output_start', 'output_end')) or
                            proof.get('qualified_current_mapping') is not True):
                            outcome = {**checkpoint, 'reason':request.get('mapping_reason') or 'exact_master_mapping_unavailable',
                                       'history_coverage_qualified':False}
                            with transaction(engine) as session:
                                complete_job(session, lease, utcnow(), outcome, status='unsupported')
                            outcomes.append({'job_id':lease.job_id, 'source':source, 'domain':job_domain,
                                             'status':'unsupported', **outcome})
                            processed += 1
                            continue
                        end = date.fromisoformat(checkpoint.get("cursor_end") or request["output_end"])
                        anchor = datetime.fromisoformat(checkpoint["request_anchor"]) if checkpoint.get("request_anchor") else utcnow()
                        api_request = KISOverseasDailyRequest(request["provider_symbol"],
                            date.fromisoformat(request["output_start"]), end, request["exchange"], captured_at=anchor)
                        response, payload = kis.overseas_daily_prices(api_request)
                        with transaction(engine) as session:
                            heartbeat_job(session, lease, utcnow(), timedelta(minutes=5))
                            outcome = {**checkpoint, **store_kis_overseas_prices(session, entity_id,
                                api_request, response, payload, raw_root, mapping_evidence=request.get("mapping_evidence"),
                                fence=lambda: heartbeat_job(session, lease, utcnow(), timedelta(minutes=5)))}
                            outcome["requested_output_end"] = request["output_end"]
                            outcome["rejected_total"] = checkpoint.get("rejected_total", 0) + outcome["rejected"]
                            outcome["source_rows_total"] = checkpoint.get("source_rows_total", 0) + outcome["source_row_count"]
                            earliest = outcome.get("cursor_earliest_session")
                            if not outcome["response_valid"] or not outcome["pagination_valid"]:
                                status, outcome["reason"] = "unsupported", "source_response_or_pagination_gap"
                            elif not outcome["traversal_complete"]:
                                if not earliest or earliest > end.isoformat():
                                    status, outcome["reason"] = "unsupported", "source_pagination_did_not_progress"
                                else:
                                    outcome["cursor_end"] = (date.fromisoformat(earliest) - timedelta(days=1)).isoformat()
                                    status = "retry"
                            elif outcome["rejected_total"]:
                                status, outcome["reason"] = "unsupported", "source_row_validation_gap"
                            elif not outcome["source_rows_total"]:
                                status, outcome["reason"] = "unsupported", "source_no_data"
                            elif outcome["source_exhausted"]:
                                outcome["reason"] = "source_exhausted_with_observed_history"
                            complete_job(session, lease, utcnow(), outcome, status=status)
                        outcomes.append({"job_id": lease.job_id, "source": source, "domain": job_domain, "status": status, **outcome})
                        processed += 1
                        continue
                    elif source != "kis":
                        status, outcome = "unsupported", {"reason": "unregistered_price_source"}
                    else:
                        end = date.fromisoformat(checkpoint.get("cursor_end") or request["output_end"])
                        api_request = KISDailyRequest(request["symbol"], date.fromisoformat(request["output_start"]), end)
                        response, payload = kis.daily_prices(api_request)
                        with transaction(engine) as session:
                            heartbeat_job(session, lease, utcnow(), timedelta(minutes=5))
                            outcome = {**checkpoint, **store_kis_prices(session, entity_id, api_request, response, payload, raw_root,
                                fence=lambda: heartbeat_job(session, lease, utcnow(), timedelta(minutes=5)))}
                            earliest = outcome.get("cursor_earliest_session")
                            outcome["rejected_total"] = checkpoint.get("rejected_total", 0) + outcome["rejected"]
                            outcome["source_rows_total"] = checkpoint.get("source_rows_total", 0) + outcome["source_row_count"]
                            if not outcome['response_valid'] or not outcome['pagination_valid']:
                                status, outcome['reason'] = 'unsupported', 'source_response_or_pagination_gap'
                            elif not outcome['traversal_complete']:
                                if not earliest or earliest > end.isoformat():
                                    status, outcome['reason'] = 'unsupported', 'source_pagination_did_not_progress'
                                else:
                                    outcome['cursor_end'] = (date.fromisoformat(earliest) - timedelta(days=1)).isoformat()
                                    status = 'retry'
                            elif outcome["rejected_total"]:
                                status, outcome["reason"] = "unsupported", "source_row_validation_gap"
                            elif not outcome['source_rows_total']:
                                status, outcome['reason'] = 'unsupported', 'source_no_data_or_validation_gap'
                            elif outcome['source_exhausted']:
                                outcome['reason'] = 'source_exhausted_with_observed_history'
                            complete_job(session, lease, utcnow(), outcome, status=status)
                        outcomes.append({"job_id": lease.job_id, "source": source, "domain": job_domain, "status": status, **outcome})
                        processed += 1
                        continue
                elif job_domain == "fundamentals":
                    with transaction(engine) as session:
                        heartbeat_job(session, lease, utcnow(), timedelta(minutes=5))
                        security = session.get(Security, entity_id)
                        if security.issuer_id is None:
                            status, outcome = "unsupported", {"reason": "explicit_issuer_mapping_required"}
                        else:
                            from .jobs import plan_job
                            discovery = plan_job(session, source=source, domain="issuer_discovery", entity_id=security.issuer_id,
                                request={"market": request["market"], "issuer_id": security.issuer_id,
                                         "output_start": request["output_start"], "output_end": request["output_end"],
                                         "manifest_id": request["manifest_id"]}, priority=2)
                            outcome = {"discovery_job_id": discovery.id, "coverage_complete": False}
                elif job_domain == "issuer_discovery":
                    outcome = discover_issuer(engine, issuer_id=entity_id, market=request["market"], request=request, dart=dart, sec=sec,
                        fence=lambda session: heartbeat_job(session, lease, utcnow(), timedelta(minutes=5)))
                elif job_domain == "filing_json":
                    outcome = collect_dart_json(engine, request, dart=dart,
                        fence=lambda session: heartbeat_job(session, lease, utcnow(), timedelta(minutes=5)))
                    status = outcome.pop("status", "succeeded")
                else:
                    outcome = collect_filing(engine, request, source=source, dart=dart, sec=sec,
                        fence=lambda session: heartbeat_job(session, lease, utcnow(), timedelta(minutes=5)))
                    status = outcome.pop("status", "succeeded")
                with transaction(engine) as session:
                    heartbeat_job(session, lease, utcnow(), timedelta(minutes=5))
                    complete_job(session, lease, utcnow(), outcome, status=status)
                outcomes.append({"job_id": lease.job_id, "source": source, "domain": job_domain, "status": status, **outcome})
                processed += 1
            except LeaseLost:
                record_lease_loss(lease,source,job_domain)
            except ProviderError as exc:
                try:
                    with transaction(engine) as session:
                        complete_job(session, lease, utcnow(), {**checkpoint, "reason": exc.code}, status="retry" if exc.retryable else "unsupported")
                except LeaseLost:
                    record_lease_loss(lease,source,job_domain)
                    continue
                outcomes.append({"job_id": lease.job_id, "source": source, "domain": job_domain, "status": "source_error", "reason": exc.code})
                processed += 1
                if source == 'opendart' and job_domain == 'filing_sql' and exc.code == '800':
                    blocked_stages[(source, job_domain)] = exc.code
                elif exc.retryable or exc.code in {"requires_credentials", "requires_identifying_user_agent", "token_unavailable", "010", "011", "012", "401", "403"}:
                    blocked_sources[source] = exc.code
                    if source in {"kis", "kis_overseas"}:
                        blocked_sources.update({peer: exc.code for peer in sources if peer in {"kis", "kis_overseas"}})
            except ValueError as exc:
                try:
                    with transaction(engine) as session:
                        complete_job(session, lease, utcnow(), {**checkpoint, "reason": type(exc).__name__}, status="unsupported")
                except LeaseLost:
                    record_lease_loss(lease,source,job_domain)
                    continue
                outcomes.append({"job_id": lease.job_id, "source": source, "domain": job_domain, "status": "unsupported", "reason": type(exc).__name__})
                processed += 1
    finally:
        http.close()
    return {"status": "blocked" if not processed and blocked_sources else "partial", "processed_this_run": processed, "elapsed_seconds": round(time.monotonic() - start, 2),
            "setup_elapsed_seconds": round(setup_elapsed, 2), "budget_semantics": "execution_excludes_queue_planning",
            "blocked_sources": blocked_sources, "scheduling_policy": "durable_lane_attempts_round_robin_v2",
            "blocked_stages": [{'source':s, 'domain':d, 'reason':reason} for (s,d),reason in sorted(blocked_stages.items())],
            "structured_dart_jobs_planned": json_planned,
            "target_scope_preserved": True, "outcomes": list(outcomes), "outcomes_truncated": processed > len(outcomes),
            "mode": mode, "requested_sources": source_filter, "identical_pending_jobs_superseded": consolidated, "repair_jobs_requeued": repair_count,
            "incremental_semantics": "resume_explicitly_planned_work", "historical_revision_scan_implemented": False, "coverage_complete": False}
