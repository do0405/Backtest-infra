"""Strict normalization helpers: no filling, unit inference, or price repair."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .domain import PriceObservation, require_aware
from .validation import validate_observation


@dataclass(frozen=True, slots=True)
class ObservationMetadata:
    """Provenance supplied by capture/contract layer, never inferred from prices."""

    id: str
    source_contract_id: str
    raw_ref: str
    source_revision: str
    available_at: datetime | None
    captured_at: datetime
    no_trade_evidence: str | None = None
    complete: bool = True
    upstream_id: str | None = None
    processing_path: str | None = None

    def __post_init__(self) -> None:
        require_aware(self.available_at, "available_at")
        require_aware(self.captured_at, "captured_at")


def decimal_value(value: str | int | Decimal | None) -> Decimal | None:
    """Parse exact provider decimals; JSON decoders should use ``parse_float=Decimal``.

    Empty/missing provider fields stay missing. Binary floats are rejected instead
    of silently changing reported precision.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (float, bool)):
        raise ValueError("binary float/bool input is not an exact decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid decimal field") from exc
    if not result.is_finite():
        raise ValueError("decimal field must be finite")
    return result


def normalize_observation(observation: PriceObservation, *, requested_basis: str | None = None,
                          requested_start: date | None = None,
                          requested_end: date | None = None) -> PriceObservation:
    """Validate already mapped inputs; return the immutable observation unchanged."""
    if requested_basis is not None and observation.context.basis != requested_basis:
        raise ValueError("response basis does not match requested basis")
    if requested_start is not None and observation.session < requested_start:
        raise ValueError("response session precedes requested start")
    if requested_end is not None and observation.session > requested_end:
        raise ValueError("response session exceeds requested end")
    issues = validate_observation(observation)
    if issues:
        raise ValueError("; ".join(f"{i.field}: {i.code}: {i.message}" for i in issues))
    return observation
