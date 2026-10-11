from collections import Counter
from collections.abc import Mapping
from datetime import datetime
import json
import re

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ..catalog.sources import ensure_contract
from ..catalog.models import Security
from ..db import new_id, utcnow
from ..ingestion.jobs import stable_hash
from ..ingestion.models import RawObject, IngestionAttempt
from ..ingestion.raw_store import RawStore
from ..releases.models import SelectionDecision, ValidationIssue
from .adapters.kis import normalize_kis_bar
from .domain import PriceContext
from .normalize import ObservationMetadata
from .models import PriceObservation
from .write_batches import PRICE_WRITE_BATCH_SIZE, flush_price_evidence


KIS_SPEC = {"endpoint": "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "raw_option": "1",
            "venue": "KRX", "feed": "kis:krx", "quantity_unit": "shares", "currency": "KRW",
            "quantity_scope": "session_total", "matched_vwap": False, "action_history_complete": False,
            "availability": "capture_only_until_verified_calendar_and_revision_evidence",
            "reference": "https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/domestic_stock/domestic_stock_functions.py"}

KIS_NORMALIZER_VERSION = "kis_domestic_source_quote_v2"


def store_kis_prices(session, security_id, request, response, payload, raw_root, *, replay_evidence=None, fence=None):
    """Append captures, or replay a caller-verified original archived capture.

    Before replay, the caller verifies the original received attempt's raw hash,
    exact request parameters, and capture timestamp. Replay provenance belongs
    only to newly inserted records; existing rows and decisions stay immutable.
    An optional fence checks the caller's lease within this page transaction.
    """
    replay_proof = None
    if replay_evidence is not None:
        original_id = replay_evidence.get("original_ingestion_attempt_id") if isinstance(replay_evidence, Mapping) else None
        if not isinstance(original_id, str) or not original_id.strip():
            raise ValueError("replay evidence requires nonempty original_ingestion_attempt_id")
        try:
            replay_proof = json.loads(json.dumps(dict(replay_evidence), allow_nan=False))
        except (TypeError, ValueError):
            raise ValueError("original_ingestion_attempt_id replay evidence must contain exact JSON values") from None
    def check_fence():
        if fence is not None:
            with session.no_autoflush:
                fence()

    check_fence()
    with session.no_autoflush:
        security = session.execute(select(Security).where(Security.id == security_id).with_for_update()).scalar_one()
    check_fence()
    with session.no_autoflush:
        contract = ensure_contract(session, source="kis", domain="prices", market="KR", specification=KIS_SPEC)
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
    attempt_id = new_id()
    attempt_evidence = {"endpoint": KIS_SPEC["endpoint"], "params": request.params()}
    if replay_proof is not None:
        attempt_evidence.update({"replay_evidence": replay_proof, "normalizer_version": KIS_NORMALIZER_VERSION})
    session.add(IngestionAttempt(id=attempt_id, source_contract_id=contract.id, request_hash=stable_hash(request.params()),
        content_hash=stored.content_hash, status="replayed" if replay_proof is not None else "received", evidence=attempt_evidence,
        captured_at=response.captured_at))
    context = PriceContext(security_id, security_id + ":KRX", "KRX", "kis:krx", "session_total", "KRW",
                           "currency_per_share", "shares", "as_traded")
    inserted, rejected, sessions, source_sessions = 0, 0, [], []
    rejection_reasons = Counter()
    supplier_zero_rows = 0
    inserted_record_ids = []
    pending_evidence = 0

    def flush_pending():
        nonlocal pending_evidence
        flush_price_evidence(session, fence)
        pending_evidence = 0

    def issue(rule, exc, *, index=None, row=None):
        nonlocal rejected, pending_evidence
        rejected += 1
        reason = str(exc)
        rejection_reasons[reason] += 1
        session.add(ValidationIssue(record_domain="price_observation", rule=rule, severity="error",
            evidence={"raw_id": raw.id, "security_id": security_id, "request": request.params(),
                "session": row.get("stck_bsop_date") if isinstance(row, Mapping) else None,
                "row_index": index, "error_type": type(exc).__name__, "reason": reason}))
        pending_evidence += 1
        if pending_evidence == PRICE_WRITE_BATCH_SIZE:
            flush_pending()

    source_rows = payload.get("output2") if isinstance(payload, Mapping) else None
    response_valid = (security.market == "KR" and response.status_code == 200 and isinstance(payload, Mapping)
        and str(payload.get("rt_cd")) == "0" and isinstance(source_rows, list))
    rows = source_rows if isinstance(source_rows, list) else []
    pagination_valid = response_valid
    parsed_rows = []
    previous_session = None
    if not response_valid:
        issue("source_response_validation", ValueError("successful KR daily response with output2 list required"))
    # Numeric rejection never determines the cursor. Parse the complete page
    # before inserting any price so malformed/overlapping pages cannot skip it.
    for index, row in enumerate(rows if response_valid else []):
        blank_fields = {"stck_bsop_date", "stck_oprc", "stck_hgpr", "stck_lwpr", "stck_clpr", "acml_vol", "acml_tr_pbmn"}
        if (isinstance(row, Mapping) and blank_fields.issubset(row)
                and all(value in (None, "") for value in row.values())):
            continue  # The supplier can pad an exhausted page with blank rows.
        try:
            source_date = row.get("stck_bsop_date") if isinstance(row, Mapping) else None
            if not isinstance(source_date, str) or not re.fullmatch(r"[0-9]{8}", source_date):
                raise ValueError("source date requires an explicit YYYYMMDD string")
            source_session = datetime.strptime(source_date, "%Y%m%d").date()
            source_sessions.append(source_session)
            if source_session > request.end:
                raise ValueError("source session exceeds requested end")
            if previous_session is not None and source_session >= previous_session:
                raise ValueError("source page dates must progress strictly backward without duplicates")
            previous_session = source_session
            parsed_rows.append((index, row, source_session))
        except (ValueError, TypeError) as exc:
            pagination_valid = False
            issue("source_session_validation", exc, index=index, row=row)
    boundary_excluded = sum(day < request.start for day in source_sessions)
    boundary_reached = pagination_valid and any(day <= request.start for day in source_sessions)
    for index, row, source_session in parsed_rows if pagination_valid else []:
        if source_session < request.start:
            # Preserve the archive as traversal evidence, never an out-of-range
            # price or a numeric rejection. This is defensive boundary handling.
            continue
        record_id = new_id()
        metadata = ObservationMetadata(record_id, contract.id, raw.id, stored.content_hash, response.captured_at, response.captured_at)
        try:
            observation = normalize_kis_bar(row, context=context, metadata=metadata, request=request,
                                             response_basis="as_traded", quantity_scope="session_total")
        except (ValueError, TypeError, KeyError) as exc:
            issue("source_row_validation", exc, index=index, row=row)
            continue
        supplier_zero = observation.volume == 0 and observation.notional == 0 and bool(observation.no_trade_evidence)
        supplier_zero_rows += int(supplier_zero)
        row_hash = stable_hash([security_id, contract.id, row])
        # Flush the previous decision/rejections with a protected heartbeat,
        # so neither prior-capture SELECT can hide a slow ORM autoflush.
        flush_pending()
        with session.no_autoflush:
            previous = session.scalar(select(PriceObservation).where(PriceObservation.security_id == security_id,
                PriceObservation.source_contract_id == contract.id, PriceObservation.session == observation.session)
                .order_by(PriceObservation.captured_at.desc(), PriceObservation.id.desc()).limit(1))
        check_fence()
        with session.no_autoflush:
            same_capture = next((item for item in session.scalars(select(PriceObservation).where(
                PriceObservation.security_id == security_id, PriceObservation.source_contract_id == contract.id,
                PriceObservation.session == observation.session, PriceObservation.captured_at == response.captured_at))
                if item.evidence.get("source_row_hash", item.signature) == row_hash), None)
        # Every distinct capture is evidence, including unchanged confirmations.
        # A later delayed revision may otherwise invalidate a collapsed repeat.
        # Replaying the same response/capture remains idempotent.
        reuse = same_capture
        signature = reuse.signature if reuse else stable_hash(["source_capture_revision_v3", row_hash, response.captured_at.isoformat()])
        values = {"id": record_id, "security_id": security_id, "source_contract_id": contract.id, "raw_id": raw.id,
            "session": observation.session, "basis": "as_traded", "feed": context.feed, "venue": context.venue,
            "trade_scope": "daily_endpoint_conditions_unverified", "currency": "KRW", "quantity_unit": "shares",
            "open": observation.open, "high": observation.high, "low": observation.low, "close": observation.close,
            "volume": observation.volume, "notional": observation.notional, "signature": signature,
            "available_at": response.captured_at, "captured_at": response.captured_at,
            "evidence": {"availability_precision": "capture_only", "historical_revision_time_unknown": True,
                         "historical_identity_verified": False,
                         "source_row_hash": row_hash, "previous_observation_id": previous.id if previous else None,
                         "observation_kind": "source_capture_event",
                         "matched_vwap_eligible": False, "action_fields": list(observation.reported_action_fields)}}
        if supplier_zero:
            values["evidence"].update({"supplier_reported_zero_transactions": True, "no_trade_status_verified": False,
                "research_only": True, "no_trade_evidence": observation.no_trade_evidence,
                "supplier_zero_transaction_evidence": {"raw_id": raw.id, "session": row["stck_bsop_date"],
                    **{name: row[name] for name in ("acml_vol", "acml_tr_pbmn", "stck_oprc", "stck_hgpr", "stck_lwpr", "stck_clpr")}}})
        if replay_proof is not None:
            values["evidence"].update({"replay_evidence": replay_proof, "normalizer_version": KIS_NORMALIZER_VERSION})
        check_fence()
        returned = session.execute(insert(PriceObservation).values(values).on_conflict_do_nothing(
            index_elements=[PriceObservation.signature]).returning(PriceObservation.id)).scalar_one_or_none()
        check_fence()
        if returned:
            inserted += 1
            inserted_record_ids.append(returned)
            session.add(SelectionDecision(record_domain="price_observation", record_id=returned, status="accepted",
                rule_version="kis_supplier_zero_transaction_structure_v1" if supplier_zero else "raw_price_structure_v1",
                evidence_grade="supplier_reported_unverified" if supplier_zero else "contract_verified",
                reason=("Supplier explicitly reports zero daily volume and amount with positive identical OHLC; raw-linked quote evidence only; "
                        "research only, trading status unverified, capture-only availability" if supplier_zero else
                        "Explicit raw daily request; structural units verified; capture-only availability"),
                actor="collector", recorded_at=utcnow()))
            pending_evidence += 1
            if pending_evidence == PRICE_WRITE_BATCH_SIZE:
                flush_pending()
        sessions.append(observation.session)
    flush_pending()
    return {"inserted": inserted, "inserted_record_ids": inserted_record_ids, "ingestion_attempt_id": attempt_id,
            "rejected": rejected, "earliest_session": min(sessions).isoformat() if sessions else None,
            "cursor_earliest_session": min(source_sessions).isoformat() if pagination_valid and source_sessions else None,
            "source_row_count": len(source_sessions), "normalized_row_coverage_complete": response_valid and pagination_valid and rejected == 0,
            "response_valid": response_valid, "pagination_valid": pagination_valid,
            "boundary_reached": boundary_reached, "boundary_excluded_count": boundary_excluded,
            "source_exhausted": pagination_valid and not source_sessions,
            "traversal_complete": pagination_valid and (boundary_reached or not source_sessions),
            "history_coverage_qualified": False, "supplier_zero_transaction_rows": supplier_zero_rows,
            "rejection_reason_counts": dict(rejection_reasons),
            "latest_session": max(sessions).isoformat() if sessions else None, "raw_hash": stored.content_hash,
            "availability_precision": "capture_only", "matched_vwap": False}
