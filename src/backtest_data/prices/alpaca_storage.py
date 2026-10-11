"""Immutable research observations from the free historical SIP daily contract."""
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ..catalog.models import Security
from ..catalog.sources import ensure_contract
from ..db import new_id, utcnow
from ..ingestion.jobs import stable_hash
from ..ingestion.models import IngestionAttempt, RawObject
from ..ingestion.raw_store import RawStore
from ..releases.models import SelectionDecision, ValidationIssue
from .adapters.alpaca import normalize_alpaca_bar
from .domain import PriceContext, require_aware
from .models import PriceObservation
from .normalize import ObservationMetadata
from .write_batches import PRICE_WRITE_BATCH_SIZE, flush_price_evidence


ALPACA_SPEC = {
    "endpoint": "/v2/stocks/{symbol}/bars", "timeframe": "1Day", "feed": "sip", "adjustment": "raw",
    "asof": "-", "basis": "as_traded", "venue": "US_CONSOLIDATED", "currency": "USD",
    "quantity_unit": "shares", "price_unit": "currency_per_share", "session_scope": "new_york_calendar_day",
    "trade_scope": "alpaca_sip_daily_condition_filtered_ohlc_general_volume",
    "daily_timestamp_timezone": "America/New_York", "calendar_qualified": False,
    "historical_identity_verified": False, "free_historical_delay_minutes": 16,
    "unclosed_calendar_days_excluded": True, "minimum_interval_seconds": 0.31,
    "advertised_history_start": "2016-01-01", "history_coverage_qualified": False,
    "availability": "capture_only_until_verified_revision_evidence", "matched_vwap": False,
    "provider_vwap": "supplier_reported_unverified_different_eligible_volume_possible",
    "action_history_complete": False, "qualification": "research_only",
    "references": ["https://docs.alpaca.markets/us/reference/stockbarsingle-1",
                   "https://docs.alpaca.markets/us/docs/market-data-faq",
                   "https://docs.alpaca.markets/us/docs/about-market-data-api"],
}


def _hashable_exact(value):
    """Keep Decimal spelling and type without ever converting through float."""
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, Mapping):
        return {key: _hashable_exact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_hashable_exact(item) for item in value]
    return value


def store_alpaca_prices(session, security_id, request, response, payload, raw_root, *, fence=None):
    """Archive each page and preserve each distinct capture of every valid bar.

    API pagination is independent of accepted bars. An exhausted API traversal
    only describes this request; it does not establish historical completeness.
    An optional fence verifies the caller's lease within the page transaction.
    """
    require_aware(response.captured_at, "captured_at")
    def check_fence():
        if fence is not None:
            with session.no_autoflush:
                fence()

    check_fence()
    with session.no_autoflush:
        security = session.execute(select(Security).where(Security.id == security_id).with_for_update()).scalar_one()
    check_fence()
    params = request.params()
    endpoint = request.endpoint()
    with session.no_autoflush:
        contract = ensure_contract(session, source="alpaca", domain="prices", market="US", specification=ALPACA_SPEC)
    check_fence()
    stored = RawStore(raw_root).put(response.content)
    check_fence()
    with session.no_autoflush:
        session.execute(insert(RawObject).values(id=new_id(), content_hash=stored.content_hash, storage_key=stored.storage_key,
            byte_count=stored.byte_count).on_conflict_do_nothing(index_elements=[RawObject.content_hash]))
    check_fence()
    with session.no_autoflush:
        raw = session.scalar(select(RawObject).where(RawObject.content_hash == stored.content_hash))
    check_fence()
    session.add(IngestionAttempt(source_contract_id=contract.id, request_hash=stable_hash([endpoint, params]),
        content_hash=stored.content_hash, status="received", captured_at=response.captured_at,
        evidence={"endpoint": endpoint, "params": params, "target_start": request.start.isoformat(),
                  "target_end": request.end.isoformat(), "request_anchor": request.captured_at.isoformat()}))

    pending_issues = 0

    def issue(rule, *, index=None):
        nonlocal pending_issues
        session.add(ValidationIssue(record_domain="price_observation", rule=rule, severity="error",
            evidence={"raw_id": raw.id, "security_id": security_id, "request": params, "row_index": index}))
        pending_issues += 1
        if pending_issues == PRICE_WRITE_BATCH_SIZE:
            flush_price_evidence(session, fence)
            pending_issues = 0

    identity_valid = (security.market == "US" and security.source_identifier == "nasdaq_directory:" + request.symbol
                      and isinstance(payload, Mapping) and payload.get("symbol") == request.symbol)
    bars = payload.get("bars") if isinstance(payload, Mapping) else None
    rows_valid = isinstance(payload, Mapping) and "bars" in payload and (bars is None or isinstance(bars, list))
    rows = bars if isinstance(bars, list) else []
    token = payload.get("next_page_token") if isinstance(payload, Mapping) else None
    token_valid = (isinstance(payload, Mapping) and "next_page_token" in payload and
                   (token is None or isinstance(token, str) and bool(token) and len(token) <= 8192))
    if token is not None and token == request.page_token:
        token_valid = False
    response_valid = identity_valid and rows_valid and token_valid
    inserted, rejected, sessions = 0, 0, []
    context = PriceContext(security_id, security_id + ":SIP", ALPACA_SPEC["venue"], "sip",
        ALPACA_SPEC["session_scope"], "USD", "currency_per_share", "shares", "as_traded")
    if not response_valid:
        rejected = max(1, len(rows))
        issue("source_identity_validation" if not identity_valid else "source_response_validation")
    else:
        normalized = []
        for index, row in enumerate(rows):
            record_id = new_id()
            metadata = ObservationMetadata(record_id, contract.id, raw.id, stored.content_hash,
                response.captured_at, response.captured_at, upstream_id="CTA_UTP_SIP", processing_path="alpaca_daily_bars")
            try:
                if not isinstance(row, Mapping):
                    raise ValueError("bar must be an object")
                observation = normalize_alpaca_bar(row, context=context, metadata=metadata, request=request,
                    response_basis="as_traded")
            except (ValueError, TypeError, KeyError):
                rejected += 1
                issue("source_row_validation", index=index)
                continue
            sessions.append(observation.session)
            row_hash = stable_hash([security_id, contract.id, request.symbol, _hashable_exact(row)])
            normalized.append((record_id, observation, row_hash))
        # A page only needs revision/replay evidence for its accepted dates.
        # Empty or rejected pages must not read this security's full history.
        check_fence()
        with session.no_autoflush:
            previous_rows = session.scalars(select(PriceObservation).where(PriceObservation.security_id == security_id,
                PriceObservation.source_contract_id == contract.id, PriceObservation.session.in_(set(sessions)))
                .order_by(PriceObservation.captured_at.desc(), PriceObservation.id.desc())).all() if sessions else []
        previous_by_session, same_capture = {}, {}
        for item in previous_rows:
            previous_by_session.setdefault(item.session, item)
            if item.captured_at == response.captured_at:
                same_capture[(item.session, item.evidence.get("source_row_hash"))] = item
        records = []
        for record_id, observation, row_hash in normalized:
            existing = same_capture.get((observation.session, row_hash))
            if existing:
                continue
            previous = previous_by_session.get(observation.session)
            signature = stable_hash(["alpaca_source_capture_revision_v1", row_hash, response.captured_at.isoformat()])
            values = {"id": record_id, "security_id": security_id, "source_contract_id": contract.id, "raw_id": raw.id,
                "session": observation.session, "basis": "as_traded", "feed": "sip", "venue": context.venue,
                "trade_scope": ALPACA_SPEC["trade_scope"], "currency": "USD", "quantity_unit": "shares",
                "open": observation.open, "high": observation.high, "low": observation.low, "close": observation.close,
                "volume": observation.volume, "notional": None, "signature": signature,
                "available_at": response.captured_at, "captured_at": response.captured_at,
                "evidence": {"availability_precision": "capture_only", "historical_revision_time_unknown": True,
                    "historical_identity_verified": False, "calendar_qualified": False, "source_symbol": request.symbol,
                    "source_timestamp": observation.source_timestamp.isoformat(), "source_row_hash": row_hash,
                    "previous_observation_id": previous.id if previous else None, "observation_kind": "source_capture_event",
                    "matched_vwap_eligible": False,
                    "provider_vwap": str(observation.provider_vwap) if observation.provider_vwap is not None else None,
                    "provider_vwap_status": "supplier_reported_unverified",
                    "provider_vwap_volume_matches_bar_volume": None,
                    "research_only": True}}
            records.append(values)
        for offset in range(0, len(records), PRICE_WRITE_BATCH_SIZE):
            # The prior-capture read can use most of a still-valid lease.
            # Refresh before writing, and reject one that already expired.
            check_fence()
            with session.no_autoflush:
                returned = session.scalars(insert(PriceObservation).values(records[offset:offset + PRICE_WRITE_BATCH_SIZE]).on_conflict_do_nothing(
                    index_elements=[PriceObservation.signature]).returning(PriceObservation.id)).all()
            check_fence()
            inserted += len(returned)
            decisions = [{"id": new_id(), "record_domain": "price_observation", "record_id": record_id, "status": "accepted",
                "rule_version": "alpaca_raw_price_structure_v1", "evidence_grade": "contract_verified",
                "reason": "Explicit raw SIP daily request; structural units verified; research capture-only availability",
                "actor": "collector", "recorded_at": utcnow()} for record_id in returned]
            if decisions:
                with session.no_autoflush:
                    session.execute(insert(SelectionDecision).values(decisions))
            check_fence()
    deferred_start = max(request.start, request.effective_end + timedelta(days=1)) if request.effective_end < request.end else None
    traversal_complete = token_valid and token is None
    empty_history = response_valid and not rows and traversal_complete
    flush_price_evidence(session, fence)
    return {"inserted": inserted, "rejected": rejected, "source_row_count": len(rows),
        "earliest_session": min(sessions).isoformat() if sessions else None,
        "latest_session": max(sessions).isoformat() if sessions else None,
        "next_page_token": token if token_valid else None, "traversal_complete": traversal_complete,
        "pagination_valid": token_valid, "response_valid": response_valid,
        "normalized_row_coverage_complete": response_valid and rejected == 0 and bool(sessions),
        "empty_history_unavailable": empty_history, "target_start": request.start.isoformat(), "target_end": request.end.isoformat(),
        "request_anchor": request.captured_at.isoformat(),
        "page_size": request.page_size,
        "effective_end": request.effective_end.isoformat(),
        "deferred_recent_start": deferred_start.isoformat() if deferred_start else None,
        "deferred_recent_end": request.end.isoformat() if deferred_start else None,
        "raw_hash": stored.content_hash, "availability_precision": "capture_only", "matched_vwap": False,
        "history_coverage_qualified": False, "calendar_qualified": False, "research_only": True}
