"""Structural price validation; diagnostics never mutate source observations."""

from dataclasses import dataclass
from decimal import Decimal

from .domain import PriceBasis, PriceObservation, finite_decimal
from .numeric import exact_difference, exact_sum, fixed_divide


@dataclass(frozen=True, slots=True)
class PriceIssue:
    code: str
    field: str
    message: str
    severity: str = "error"


def validate_observation(observation: PriceObservation) -> tuple[PriceIssue, ...]:
    """Return structural errors while preserving absent volume/notional as absent."""
    issues: list[PriceIssue] = []
    context = observation.context
    for name in ("security_id", "listing_id", "venue", "feed", "session_scope", "currency", "price_unit", "quantity_unit", "basis"):
        if not getattr(context, name):
            issues.append(PriceIssue("missing_context", name, f"{name} must be explicit"))
    if context.basis not in set(PriceBasis):
        issues.append(PriceIssue("unknown_basis", "basis", "price basis is unsupported"))
    if context.basis != PriceBasis.AS_TRADED and not context.share_basis:
        issues.append(PriceIssue("missing_share_basis", "share_basis", "adjusted input requires its share basis"))
    for name in ("open", "high", "low", "close", "volume", "notional", "provider_vwap"):
        value = getattr(observation, name)
        if value is not None and (not isinstance(value, Decimal) or not value.is_finite()):
            issues.append(PriceIssue("nonfinite", name, "value must be a finite Decimal"))
        elif value is not None and value < 0:
            code = "negative_quantity" if name == "volume" else "negative_notional" if name == "notional" else "negative_price"
            issues.append(PriceIssue(code, name, "value must be nonnegative"))
    prices = [observation.open, observation.high, observation.low, observation.close]
    if any(p is None for p in prices) and not all(p is None for p in prices):
        issues.append(PriceIssue("incomplete_ohlc", "ohlc", "OHLC must be complete or wholly absent"))
    elif all(p is None for p in prices) and not observation.no_trade_evidence:
        issues.append(PriceIssue("missing_ohlc", "ohlc", "absent OHLC requires no-trade evidence"))
    elif all(isinstance(p, Decimal) and p.is_finite() for p in prices):
        o, h, low, c = prices
        if not (low <= min(o, c) <= max(o, c) <= h):
            issues.append(PriceIssue("invalid_ohlc", "ohlc", "low <= open/close <= high must hold"))
    if observation.volume == 0 and not observation.no_trade_evidence:
        issues.append(PriceIssue("zero_without_evidence", "volume", "zero volume requires explicit no-trade evidence"))
    if observation.volume == 0 and observation.notional not in (None, Decimal(0)):
        issues.append(PriceIssue("zero_volume_notional", "notional", "no trades cannot have positive notional"))
    if not observation.complete:
        issues.append(PriceIssue("incomplete_session", "complete", "incomplete session is not a completed daily observation"))
    for name in ("id", "source_contract_id", "raw_ref", "source_revision"):
        if not getattr(observation, name):
            issues.append(PriceIssue("missing_provenance", name, f"{name} must be explicit"))
    return tuple(issues)


@dataclass(frozen=True, slots=True)
class ComparisonContract:
    """Verified absolute tolerances from tick/rounding and quantity contracts.

    No percentage tolerance is supplied. ``price_quantum`` values describe
    source rounding increments; summed half-quanta bound their rounding error.
    Independence must be explicitly verified across actual processing paths.
    """

    evidence_ref: str
    tick_size: Decimal | None = None
    left_price_quantum: Decimal | None = None
    right_price_quantum: Decimal | None = None
    quantity_tolerance: Decimal | None = None
    independent_paths_verified: bool = False

    def __post_init__(self) -> None:
        if not self.evidence_ref:
            raise ValueError("comparison tolerance requires evidence")
        for name in ("tick_size", "left_price_quantum", "right_price_quantum"):
            finite_decimal(getattr(self, name), name, positive=True)
        finite_decimal(self.quantity_tolerance, "quantity_tolerance")


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    status: str
    observation_ids: tuple[str, str]
    issues: tuple[PriceIssue, ...]
    independent: bool
    evidence_ref: str


def compare_observations(left: PriceObservation, right: PriceObservation, *,
                         contract: ComparisonContract) -> ComparisonResult:
    """Diagnose equivalent observations; never delete spikes or repair originals."""
    ids = (left.id, right.id)
    independent = bool(contract.independent_paths_verified
                       and left.source_contract_id != right.source_contract_id
                       and left.upstream_id and right.upstream_id
                       and left.processing_path and right.processing_path
                       and left.processing_path != right.processing_path)
    if left.context != right.context or left.session != right.session:
        return ComparisonResult("incomparable", ids, (PriceIssue("incompatible_context", "context", "session/feed/basis/units differ"),), independent, contract.evidence_ref)
    structural = validate_observation(left) + validate_observation(right)
    if structural:
        return ComparisonResult("unresolved", ids, structural, independent, contract.evidence_ref)
    issues: list[PriceIssue] = []
    mismatch = False
    tolerance = contract.tick_size
    if contract.left_price_quantum is not None and contract.right_price_quantum is not None:
        rounding = fixed_divide(exact_sum((contract.left_price_quantum, contract.right_price_quantum)), Decimal(2))
        tolerance = max(tolerance or Decimal(0), rounding)
    for name in ("open", "high", "low", "close"):
        a, b = getattr(left, name), getattr(right, name)
        if a is None or b is None:
            issues.append(PriceIssue("missing_price", name, "price comparison is unavailable"))
        elif a != b:
            if tolerance is None:
                issues.append(PriceIssue("unverified_price_precision", name, "verified tick or both reporting quanta are required"))
            elif exact_difference(a, b).copy_abs() > tolerance:
                mismatch = True
                issues.append(PriceIssue("price_mismatch", name, "difference exceeds verified absolute tolerance"))
    if left.volume is None or right.volume is None:
        issues.append(PriceIssue("missing_quantity", "volume", "missing volume is unknown, not evidence of suspension"))
    elif left.volume != right.volume:
        if contract.quantity_tolerance is None:
            issues.append(PriceIssue("unverified_quantity_precision", "volume", "quantity tolerance is unverified"))
        elif exact_difference(left.volume, right.volume).copy_abs() > contract.quantity_tolerance:
            mismatch = True
            issues.append(PriceIssue("quantity_mismatch", "volume", "difference exceeds source-contract tolerance"))
    status = "mismatch" if mismatch else "unresolved" if issues else "agree"
    return ComparisonResult(status, ids, tuple(issues), independent, contract.evidence_ref)
