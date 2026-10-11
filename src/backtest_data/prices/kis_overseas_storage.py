"""Auditable KIS US raw daily captures with verified current master mappings."""

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
import re

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ..catalog.models import Security
from ..catalog.sources import ensure_contract
from ..db import new_id, utcnow
from ..ingestion.jobs import stable_hash
from ..ingestion.models import IngestionAttempt, RawObject
from ..ingestion.raw_store import RawStore
from ..releases.models import SelectionDecision, ValidationIssue
from .adapters.kis_overseas import ENDPOINT, NEW_YORK, TR_ID, normalize_kis_overseas_bar, reported_precision, source_session
from .domain import PriceContext, require_aware
from .models import PriceObservation
from .normalize import ObservationMetadata
from .write_batches import PRICE_WRITE_BATCH_SIZE, flush_price_evidence


KIS_OVERSEAS_SPEC = {
    "endpoint": ENDPOINT, "tr_id": TR_ID, "period_option": "0", "raw_option": "0",
    "basis": "as_traded", "market": "US", "venues": ["NAS", "NYS", "AMS"],
    "feed": "kis:us:<explicit_exchange>", "currency": "USD", "quantity_unit": "shares",
    "price_unit": "currency_per_share", "quantity_scope": "session_total",
    "session_scope": "daily_endpoint_session_conditions_unverified",
    "trade_scope": "kis_overseas_daily_conditions_unverified",
    "turnover_volume_eligibility_verified": False, "matched_vwap": False,
    "action_history_complete": False, "historical_identity_verified": False,
    "history_coverage_qualified": False, "calendar_qualified": False,
    "current_mapping": "explicit_verified_master_and_catalog_intersection_required",
    "availability": "capture_only_until_verified_revision_evidence",
    "unclosed_new_york_calendar_days_excluded": True, "qualification": "research_only",
    "zdiv": "reported_precision_evidence_no_rescaling_of_decimal_prices",
    "pagination": "strictly_descending_source_dates_explicit_start_boundary_no_page_size_exhaustion_inference",
    "references": [
        "https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/overseas_stock/dailyprice/dailyprice.py",
        "https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/overseas_stock/dailyprice/chk_dailyprice.py",
    ],
}


def _hashable_exact(value):
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, Mapping):
        return {key: _hashable_exact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_hashable_exact(item) for item in value]
    return value


def _mapping_valid(proof, security_id, request):
    if not isinstance(proof, Mapping):
        return False
    exact = (proof.get("market") == "US" and proof.get("security_id") == security_id
        and proof.get("provider_symbol") == request.symbol and proof.get("exchange") == request.exchange
        and proof.get("qualified_current_mapping") is True
        and isinstance(proof.get("source"), str) and bool(proof["source"])
        and isinstance(proof.get("realtime_symbol"), str) and bool(proof["realtime_symbol"]))
    hash_ref = any(isinstance(proof.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", proof[key])
        for key in ("source_directory_hash", "master_hash"))
    object_ref = any(isinstance(proof.get(key), str) and bool(proof[key])
        for key in ("master_raw_id", "catalog_observation_id"))
    observation_ids = proof.get("catalog_observation_ids")
    list_ref = (isinstance(observation_ids, list) and bool(observation_ids)
        and all(isinstance(value, str) and bool(value) for value in observation_ids))
    return bool(exact and (hash_ref or object_ref or list_ref))


def store_kis_overseas_prices(session, security_id, request, response, payload, raw_root, *, mapping_evidence=None, fence=None):
    """Archive one page, validate its identity/order, then retain each raw capture.

    Mapping evidence establishes only the current provider spelling and venue.
    A date before the requested start is an explicit traversal boundary. It is
    archived and counted but never inserted as an out-of-range observation.
    Neither a short page nor reaching that boundary certifies historical coverage.
    """
    if not isinstance(response.captured_at, datetime):
        raise ValueError("captured_at requires a timezone-aware timestamp")
    require_aware(response.captured_at, "captured_at")
    captured_at = response.captured_at.astimezone(timezone.utc)
    def check_fence():
        if fence is not None:
            with session.no_autoflush:
                fence()

    check_fence()
    with session.no_autoflush:
        security = session.execute(select(Security).where(Security.id == security_id).with_for_update()).scalar_one()
    check_fence()
    params = request.params()
    with session.no_autoflush:
        contract = ensure_contract(session, source="kis_overseas", domain="prices", market="US",
            specification=KIS_OVERSEAS_SPEC)
    check_fence()
    stored = RawStore(raw_root).put(response.content)
    check_fence()
    with session.no_autoflush:
        session.execute(insert(RawObject).values(id=new_id(), content_hash=stored.content_hash,
            storage_key=stored.storage_key, byte_count=stored.byte_count)
            .on_conflict_do_nothing(index_elements=[RawObject.content_hash]))
    check_fence()
    with session.no_autoflush:
        raw = session.scalar(select(RawObject).where(RawObject.content_hash == stored.content_hash))
    check_fence()
    mapping_valid = security.market == "US" and _mapping_valid(mapping_evidence, security_id, request)
    session.add(IngestionAttempt(source_contract_id=contract.id,
        request_hash=stable_hash([ENDPOINT, params]), content_hash=stored.content_hash,
        status="received", captured_at=captured_at,
        evidence={"endpoint": ENDPOINT, "tr_id": TR_ID, "params": params,
            "target_start": request.start.isoformat(), "target_end": request.end.isoformat(),
            "request_anchor": request.captured_at.isoformat(), "mapping_verified": mapping_valid,
            "current_mapping_evidence": _hashable_exact(mapping_evidence) if isinstance(mapping_evidence, Mapping) else None}))

    pending_issues = 0

    def issue(rule, *, index=None):
        nonlocal pending_issues
        session.add(ValidationIssue(record_domain="price_observation", rule=rule, severity="error",
            evidence={"raw_id": raw.id, "security_id": security_id, "request": params, "row_index": index}))
        pending_issues += 1
        if pending_issues == PRICE_WRITE_BATCH_SIZE:
            flush_price_evidence(session, fence)
            pending_issues = 0

    header = payload.get("output1") if isinstance(payload, Mapping) else None
    source_rows = payload.get("output2") if isinstance(payload, Mapping) else None
    rows_valid = isinstance(source_rows, list)
    rows = source_rows if rows_valid else []
    header_valid = isinstance(header, Mapping)
    if header_valid:
        try:
            reported_precision(header.get("zdiv"))
        except ValueError:
            header_valid = False
    identity_valid = (mapping_valid and header_valid
        and header.get("rsym") == mapping_evidence["realtime_symbol"])
    response_valid = (isinstance(payload, Mapping) and response.status_code == 200
        and payload.get("rt_cd") == "0" and rows_valid and identity_valid
        and captured_at >= request.captured_at)

    # Parse every source date before normalizing any price. Rejected numeric
    # rows still contribute to the backward cursor; an unreadable date forbids
    # the parent from advancing past a potentially skipped source row.
    source_sessions = []
    pagination_valid = response_valid
    previous_session = None
    if response_valid:
        for index, row in enumerate(rows):
            try:
                day = source_session(row.get("xymd") if isinstance(row, Mapping) else None)
                source_sessions.append(day)
                if (day > request.end or day >= captured_at.astimezone(NEW_YORK).date()
                        or previous_session is not None and day >= previous_session):
                    raise ValueError("daily page dates exceed the anchor or do not progress strictly backward")
                previous_session = day
            except (ValueError, TypeError):
                pagination_valid = False
                issue("source_session_validation", index=index)

    inserted, rejected, accepted_sessions = 0, 0, []
    in_range = sum(request.start <= day <= request.end for day in source_sessions)
    boundary_excluded = sum(day < request.start for day in source_sessions)
    boundary_reached = pagination_valid and any(day <= request.start for day in source_sessions)
    if not response_valid:
        rejected = max(1, len(rows))
        issue("source_identity_validation" if not identity_valid else "source_response_validation")
    elif not pagination_valid:
        # A malformed page is retained as evidence, never partly accepted as
        # a valid daily sequence. The parent must retry or expose the gap.
        rejected = max(1, len(rows))
    else:
        normalized = []
        context = PriceContext(security_id, security_id + ":" + request.exchange, request.exchange,
            "kis:us:" + request.exchange, "session_total", "USD", "currency_per_share", "shares", "as_traded")
        for index, (row, day) in enumerate(zip(rows, source_sessions, strict=True)):
            if day < request.start:
                continue
            record_id = new_id()
            metadata = ObservationMetadata(record_id, contract.id, raw.id, stored.content_hash,
                captured_at, captured_at, processing_path="kis_overseas_dailyprice")
            try:
                if "rsym" in row and row["rsym"] != header["rsym"]:
                    raise ValueError("row realtime symbol contradicts the verified response identity")
                if "zdiv" in row and int(reported_precision(row["zdiv"])) != int(header["zdiv"]):
                    raise ValueError("row reporting precision contradicts the response precision")
                observation = normalize_kis_overseas_bar(row, context=context, metadata=metadata, request=request)
            except (ValueError, TypeError, KeyError):
                rejected += 1
                issue("source_row_validation", index=index)
                continue
            accepted_sessions.append(day)
            row_hash = stable_hash([security_id, contract.id, request.exchange, request.symbol, _hashable_exact(row)])
            normalized.append((record_id, observation, day, row_hash))
        check_fence()
        with session.no_autoflush:
            prior_rows = session.scalars(select(PriceObservation).where(
                PriceObservation.security_id == security_id, PriceObservation.source_contract_id == contract.id,
                PriceObservation.session.in_(set(accepted_sessions)), PriceObservation.captured_at <= captured_at)
                .order_by(PriceObservation.captured_at.desc(), PriceObservation.id.desc())).all() if accepted_sessions else []
        previous_by_session, same_capture = {}, {}
        for item in prior_rows:
            if item.captured_at < captured_at:
                previous_by_session.setdefault(item.session, item)
            if item.captured_at == captured_at:
                same_capture[(item.session, item.evidence.get("source_row_hash"))] = item
        records = []
        for record_id, observation, day, row_hash in normalized:
            if (day, row_hash) in same_capture:
                continue
            previous = previous_by_session.get(day)
            signature = stable_hash(["kis_overseas_source_capture_revision_v1", row_hash, captured_at.isoformat()])
            values = {"id": record_id, "security_id": security_id, "source_contract_id": contract.id,
                "raw_id": raw.id, "session": day, "basis": "as_traded", "feed": context.feed, "venue": context.venue,
                "trade_scope": KIS_OVERSEAS_SPEC["trade_scope"], "currency": "USD", "quantity_unit": "shares",
                "open": observation.open, "high": observation.high, "low": observation.low, "close": observation.close,
                "volume": observation.volume, "notional": observation.notional, "signature": signature,
                "available_at": captured_at, "captured_at": captured_at,
                "evidence": {"availability_precision": "capture_only", "historical_revision_time_unknown": True,
                    "historical_identity_verified": False, "calendar_qualified": False, "source_symbol": request.symbol,
                    "source_row_hash": row_hash, "previous_observation_id": previous.id if previous else None,
                    "observation_kind": "source_capture_event", "matched_vwap_eligible": False,
                    "turnover_volume_eligibility_verified": False, "action_history_complete": False,
                    "rsym": header["rsym"], "zdiv": header["zdiv"], "research_only": True,
                    "current_mapping_evidence": _hashable_exact(mapping_evidence)}}
            records.append(values)
        for offset in range(0, len(records), PRICE_WRITE_BATCH_SIZE):
            # Do not begin the first write on the remainder of a slow read's
            # lease; the caller's fence still refuses expired ownership.
            check_fence()
            with session.no_autoflush:
                returned = session.scalars(insert(PriceObservation).values(records[offset:offset + PRICE_WRITE_BATCH_SIZE])
                    .on_conflict_do_nothing(index_elements=[PriceObservation.signature])
                    .returning(PriceObservation.id)).all()
            check_fence()
            inserted += len(returned)
            decisions = [{"id": new_id(), "record_domain": "price_observation", "record_id": identity,
                "status": "accepted", "rule_version": "kis_overseas_raw_price_structure_v1",
                "evidence_grade": "contract_verified",
                "reason": "Explicit overseas raw daily request and verified current mapping; structural units validated; capture-only availability",
                "actor": "collector", "recorded_at": utcnow()} for identity in returned]
            if decisions:
                with session.no_autoflush:
                    session.execute(insert(SelectionDecision).values(decisions))
            check_fence()
    empty_history = response_valid and pagination_valid and not rows
    flush_price_evidence(session, fence)
    return {"inserted": inserted, "rejected": rejected, "source_row_count": len(rows),
        "processed_in_range": in_range, "boundary_excluded_count": boundary_excluded,
        "boundary_reached": boundary_reached,
        "earliest_session": min(accepted_sessions).isoformat() if accepted_sessions else None,
        "latest_session": max(accepted_sessions).isoformat() if accepted_sessions else None,
        "cursor_earliest_session": min(source_sessions).isoformat() if source_sessions else None,
        "response_valid": bool(response_valid), "pagination_valid": bool(pagination_valid),
        "traversal_complete": bool(pagination_valid and (empty_history or boundary_reached)),
        "source_exhausted": bool(empty_history), "empty_history_unavailable": bool(empty_history),
        "normalized_row_coverage_complete": bool(response_valid and pagination_valid and rejected == 0 and in_range),
        "target_start": request.start.isoformat(), "target_end": request.end.isoformat(),
        "effective_end": request.end.isoformat(), "request_anchor": request.captured_at.isoformat(),
        "raw_hash": stored.content_hash, "availability_precision": "capture_only", "matched_vwap": False,
        "history_coverage_qualified": False, "historical_identity_verified": False,
        "calendar_qualified": False, "research_only": True}
