"""Immutable price inputs with explicit units and economic/source semantics."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class HistoryMode(StrEnum):
    RECONSTRUCTED_PUBLIC = "reconstructed_public"
    OBSERVED_SYSTEM = "observed_system"


class PriceBasis(StrEnum):
    AS_TRADED = "as_traded"
    SPLIT_ADJUSTED = "split_adjusted"
    TOTAL_RETURN = "total_return"


def require_aware(value: datetime | None, field: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field} requires a timezone-aware timestamp")


def finite_decimal(value: Decimal | None, field: str, *, positive: bool = False) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field} must be a finite Decimal")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{field} must be {'positive' if positive else 'nonnegative'}")


@dataclass(frozen=True, slots=True)
class PriceContext:
    """Identity and compatible meaning; none of these fields are guessed.

    ``session_scope`` distinguishes regular/full/overnight sessions. ``share_basis``
    is required for adjusted values (for example ``as_of:2024-01-05``).
    A source contract belongs on each observation so independent contracts with
    equivalent economic meanings can still be compared.
    """

    security_id: str
    listing_id: str
    venue: str
    feed: str
    session_scope: str
    currency: str
    price_unit: str
    quantity_unit: str
    basis: str
    share_basis: str | None = None


@dataclass(frozen=True, slots=True)
class PriceObservation:
    """One immutable source revision, including unknown quantities as ``None``.

    ``provider_vwap`` is preserved evidence, never certified matched statistics.
    ``captured_at`` does not imply a historical selection decision existed.
    """

    id: str
    context: PriceContext
    session: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None
    notional: Decimal | None
    source_contract_id: str
    raw_ref: str
    source_revision: str
    available_at: datetime | None
    captured_at: datetime
    no_trade_evidence: str | None = None
    complete: bool = True
    provider_vwap: Decimal | None = None
    upstream_id: str | None = None
    processing_path: str | None = None
    decision_recorded_at: datetime | None = None
    source_timestamp: datetime | None = None
    reported_action_fields: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for field in ("available_at", "captured_at", "decision_recorded_at", "source_timestamp"):
            require_aware(getattr(self, field), field)


def visible_at(*, available_at: datetime | None, captured_at: datetime,
               as_of: datetime, history_mode: str,
               decision_recorded_at: datetime | None = None,
               materialized_at: datetime | None = None,
               require_materialized: bool = False) -> bool:
    """Apply public/observed cutoff gates without backdating later decisions."""
    mode = HistoryMode(history_mode)
    require_aware(as_of, "as_of")
    if available_at is None or available_at > as_of:
        return False
    if mode == HistoryMode.OBSERVED_SYSTEM:
        if captured_at > as_of or decision_recorded_at is None or decision_recorded_at > as_of:
            return False
        if require_materialized and (materialized_at is None or materialized_at > as_of):
            return False
    return True
