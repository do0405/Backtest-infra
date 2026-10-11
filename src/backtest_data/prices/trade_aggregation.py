"""Matched-statistic aggregation from identified trade/correction/cancel events."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256

from .domain import HistoryMode, PriceContext, finite_decimal, require_aware
from .numeric import exact_multiply, exact_sum, numeric_signature
from .vwap import DailyVWAPStat


@dataclass(frozen=True, slots=True)
class TradeEvent:
    """A provider event and stable logical trade identity, with eligibility evidence.

    A correction supplies the full replacement price, quantity, and eligibility.
    ``sequence`` orders source events for this trade, not ingestion arrival time.
    Unknown condition eligibility is rejected rather than included by default.
    """

    event_id: str
    trade_id: str
    context: PriceContext
    session: date
    event_type: str
    sequence: int
    price: Decimal | None
    quantity: Decimal | None
    eligible: bool
    trade_scope: str
    source_contract_id: str
    raw_ref: str
    available_at: datetime | None
    captured_at: datetime

    def __post_init__(self) -> None:
        require_aware(self.available_at, "available_at")
        require_aware(self.captured_at, "captured_at")
        if not all((self.event_id, self.trade_id, self.trade_scope, self.source_contract_id, self.raw_ref)):
            raise ValueError("unresolved trade/event identity or provenance")
        if self.event_type not in {"trade", "correct", "cancel"} or self.sequence < 1:
            raise ValueError("unsupported trade event type or sequence")
        if type(self.eligible) is not bool:
            raise ValueError("trade condition eligibility must be explicit")
        finite_decimal(self.price, "price", positive=True)
        finite_decimal(self.quantity, "quantity", positive=True)
        if self.event_type != "cancel" and (self.price is None or self.quantity is None):
            raise ValueError("trade/correction requires price and quantity")


def aggregate_trades(events: Iterable[TradeEvent], *, context: PriceContext, session: date,
                     as_of: datetime, trade_scope: str, source_contract_id: str, raw_ref: str,
                     materialized_at: datetime, complete_evidence: str | None,
                     history_mode: str = "reconstructed_public", archive_mode: str = "event_history",
                     statistic_id: str | None = None, revision: int = 1,
                     decision_recorded_at: datetime | None = None,
                     completeness_available_at: datetime | None = None,
                     completeness_captured_at: datetime | None = None) -> DailyVWAPStat:
    """Deduplicate event IDs and apply only cutoff-visible identified corrections.

    Completeness and condition eligibility must be certified by the caller's
    approved source contract. ``corrected_state_only`` archives may reconstruct
    current corrected values but cannot certify historical observed replay.
    This builds a statistic now; observed query access still requires a recorded
    selection and materialization at or before its query cutoff.

    ``completeness_available_at`` must be the verified full-session/archive
    completion time INCLUDING source delay. It is required even for an empty
    archive; the last trade timestamp is not evidence that the session ended.
    ``completeness_captured_at`` records when that evidence was captured. Both
    gate the output's corresponding times. Unknown availability on an eligible
    trade or correction/cancel produces missing N/D, never an exact partial sum
    or no-trade certification.
    """
    mode = HistoryMode(history_mode)
    require_aware(as_of, "as_of")
    require_aware(materialized_at, "materialized_at")
    if not complete_evidence:
        raise ValueError("complete session archive evidence is required")
    if completeness_available_at is None or completeness_captured_at is None:
        raise ValueError("session archive completeness availability and capture times are required")
    require_aware(completeness_available_at, "completeness_available_at")
    require_aware(completeness_captured_at, "completeness_captured_at")
    if completeness_available_at > as_of or (mode == HistoryMode.OBSERVED_SYSTEM and completeness_captured_at > as_of):
        raise ValueError("session archive completeness is not available at cutoff")
    if archive_mode not in {"event_history", "corrected_state_only"}:
        raise ValueError("unsupported archive mode")
    if mode == HistoryMode.OBSERVED_SYSTEM and archive_mode != "event_history":
        raise ValueError("historical observed replay unsupported by corrected-state-only archive")
    if context.basis != "as_traded" or context.quantity_unit != "shares" or context.price_unit != "currency_per_share":
        raise ValueError("trade context must be as-traded share units")
    unique: dict[str, TradeEvent] = {}
    for event in events:
        if event.context != context or event.session != session or event.trade_scope != trade_scope or event.source_contract_id != source_contract_id:
            raise ValueError("incompatible trade context, session, source, or scope")
        prior = unique.get(event.event_id)
        if prior is not None and prior != event:
            raise ValueError("conflicting trade event identity")
        unique[event.event_id] = event
    unknown = [e for e in unique.values() if e.available_at is None
               and (e.eligible or e.event_type in {"correct", "cancel"})]
    visible = [e for e in unique.values() if e.available_at is not None and e.available_at <= as_of
               and (mode != HistoryMode.OBSERVED_SYSTEM or e.captured_at <= as_of)]
    trades: dict[str, list[TradeEvent]] = {}
    for event in visible:
        trades.setdefault(event.trade_id, []).append(event)
    numerator_terms: list[Decimal] = []
    denominator_terms: list[Decimal] = []
    for logical_events in trades.values():
        ordered = sorted(logical_events, key=lambda e: e.sequence)
        if len({e.sequence for e in ordered}) != len(ordered):
            raise ValueError("conflicting trade sequence")
        if ordered[0].event_type != "trade" or any(e.event_type == "trade" for e in ordered[1:]):
            raise ValueError("unresolved correction/cancel target or duplicate original trade")
        latest = ordered[-1]
        if latest.event_type != "cancel" and latest.eligible:
            numerator_terms.append(exact_multiply(latest.price, latest.quantity))
            denominator_terms.append(latest.quantity)
    numerator, denominator = exact_sum(numerator_terms), exact_sum(denominator_terms)
    relevant = visible + unknown
    event_ids = tuple(e.event_id for e in sorted(relevant, key=lambda e: (e.trade_id, e.sequence)))
    signature = numeric_signature(context, session, source_contract_id, raw_ref, trade_scope, archive_mode, mode,
                                  completeness_available_at, completeness_captured_at,
                                  tuple(sorted(relevant, key=lambda e: (e.trade_id, e.sequence))),
                                  None if unknown else numerator, None if unknown else denominator)
    identity = statistic_id or sha256(signature.encode()).hexdigest()
    available = max([completeness_available_at] + [e.available_at for e in visible])
    captured = max([completeness_captured_at] + [e.captured_at for e in relevant])
    if materialized_at < captured or materialized_at < available:
        raise ValueError("materialized_at cannot precede capture/availability of used inputs")
    return DailyVWAPStat(identity, context, session, None if unknown else numerator, None if unknown else denominator, trade_scope,
                         "trade_aggregated", source_contract_id, raw_ref, revision,
                         available, captured, materialized_at, decision_recorded_at,
                         no_trade_evidence=complete_evidence if denominator == 0 and not unknown else None,
                         historical_replay_supported=archive_mode == "event_history",
                         input_event_ids=event_ids,
                         incomplete_reason="unknown_trade_availability" if unknown else None,
                         input_signature=signature)
