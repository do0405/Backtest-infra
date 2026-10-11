"""Share-basis transformations and a separate gross return benchmark policy."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal

from .actions import ActionCoverage, CorporateAction, action_application_date, has_action_coverage, select_visible_actions
from .domain import PriceObservation, visible_at
from .normalize import normalize_observation
from .numeric import NUMERIC_POLICY, exact_multiply, exact_sum, fixed_divide, fixed_round, numeric_signature


@dataclass(frozen=True, slots=True)
class AdjustedPrice:
    observation: PriceObservation | None
    original_id: str
    factor: Decimal | None
    action_ids: tuple[str, ...]
    rule_version: str = "split_adjustment_v1"
    reason: str | None = None
    action_coverage_ref: str | None = None
    numeric_policy: str = NUMERIC_POLICY
    input_signature: str | None = None
    quantity_factor: Decimal | None = None


def split_factor(*, security_id: str, session: date, basis_session: date,
                 actions: Iterable[CorporateAction], as_of: datetime,
                 history_mode: str = "reconstructed_public",
                 decision_times: Mapping[str, datetime] | None = None,
                 action_coverage: ActionCoverage | None = None) -> tuple[Decimal, tuple[str, ...]]:
    """Return the rounded inverse share ratio; quantities must use the exact ratio.

    The inverse uses the versioned 50-digit policy. Use
    ``split_quantity_factor`` for exact share-volume transformations.
    """
    ratio, ids = split_quantity_factor(security_id=security_id, session=session, basis_session=basis_session,
                                       actions=actions, as_of=as_of, history_mode=history_mode,
                                       decision_times=decision_times, action_coverage=action_coverage)
    return fixed_divide(Decimal(1), ratio), ids


def split_quantity_factor(*, security_id: str, session: date, basis_session: date,
                          actions: Iterable[CorporateAction], as_of: datetime,
                          history_mode: str = "reconstructed_public",
                          decision_times: Mapping[str, datetime] | None = None,
                          action_coverage: ActionCoverage | None = None) -> tuple[Decimal, tuple[str, ...]]:
    """Exact product of new-shares/old-shares ratios under verified action coverage."""
    if basis_session < session or basis_session > as_of.date():
        raise ValueError("basis session must be between input session and as_of")
    action_rows = tuple(actions)
    if not has_action_coverage(action_coverage, security_id=security_id, from_session=session,
                               through_session=basis_session, as_of=as_of, history_mode=history_mode, actions=action_rows):
        raise ValueError("action_coverage_unavailable")
    selected = select_visible_actions(action_rows, as_of=as_of, history_mode=history_mode,
                                      decision_times=decision_times, effective_by=basis_session)
    relevant = [a for a in selected if a.security_id == security_id and a.action_type == "split" and session < a.effective_date <= basis_session]
    # Independent source copies of the same economic event need prior selection.
    if len({a.event_id for a in relevant}) != len(relevant):
        raise ValueError("multiple source revisions of one split require an action selection decision")
    ratio = Decimal(1)
    for action in relevant:
        ratio = exact_multiply(ratio, action.split_ratio)
    return ratio, tuple(a.id for a in relevant)


def split_adjust(observation: PriceObservation, actions: Iterable[CorporateAction], *,
                 as_of: datetime, basis_session: date, history_mode: str = "reconstructed_public",
                 decision_times: Mapping[str, datetime] | None = None,
                 action_coverage: ActionCoverage | None = None) -> AdjustedPrice:
    """Transform P'=P/R, V'=V*R, preserving notional and missing quantities.

    R (new/old shares) and transformed quantities are exact. Repeating prices
    and the reported inverse ``factor`` use 50 significant HALF_EVEN digits;
    ``quantity_factor`` preserves the exact ratio instead of its rounded inverse.

    An adjusted/provider total-return input is never adjusted again as if raw.
    Input visibility is caller-independent and enforced here.
    Unsupported complex events crossing the share-basis interval return a null
    derived observation and reason; the original observation stays available.
    A verified ``ActionCoverage`` spanning input through basis is mandatory,
    including when ``actions=[]``. Missing/incomplete/invisible coverage yields
    ``action_coverage_unavailable``; an empty list never certifies factor one.
    Output availability/capture include the coverage and used action evidence,
    so a derived observation cannot inherit the raw input's earlier timestamps.
    """
    normalize_observation(observation, requested_basis="as_traded")
    if observation.context.quantity_unit != "shares" or observation.context.price_unit != "currency_per_share":
        raise ValueError("split adjustment requires explicit shares and currency_per_share")
    if not visible_at(available_at=observation.available_at, captured_at=observation.captured_at,
                      as_of=as_of, history_mode=history_mode, decision_recorded_at=observation.decision_recorded_at):
        raise ValueError("price observation is not visible at cutoff")
    action_rows = tuple(actions)
    if basis_session < observation.session or basis_session > as_of.date():
        raise ValueError("basis session must be between input session and as_of")
    if not has_action_coverage(action_coverage, security_id=observation.context.security_id,
                               from_session=observation.session, through_session=basis_session,
                               as_of=as_of, history_mode=history_mode, actions=action_rows):
        return AdjustedPrice(None, observation.id, None, (), reason="action_coverage_unavailable")
    selected = select_visible_actions(action_rows, as_of=as_of, history_mode=history_mode,
                                      decision_times=decision_times, effective_by=basis_session)
    unsupported = tuple(a.id for a in selected if a.security_id == observation.context.security_id
                        and observation.session < a.effective_date <= basis_session
                        and a.action_type not in {"split", "cash_dividend"})
    if unsupported:
        return AdjustedPrice(None, observation.id, None, unsupported, reason="unsupported_action", action_coverage_ref=action_coverage.evidence_ref)
    ratio, ids = split_quantity_factor(security_id=observation.context.security_id, session=observation.session,
                               basis_session=basis_session, actions=action_rows, as_of=as_of,
                               history_mode=history_mode, decision_times=decision_times, action_coverage=action_coverage)
    factor = fixed_divide(Decimal(1), ratio)
    prices = {name: None if getattr(observation, name) is None else fixed_divide(getattr(observation, name), ratio)
              for name in ("open", "high", "low", "close", "provider_vwap")}
    context = replace(observation.context, basis="split_adjusted", share_basis=f"as_of:{basis_session.isoformat()}")
    used_actions = [a for a in selected if a.id in ids]
    available = max([observation.available_at, action_coverage.available_at] + [a.available_at for a in used_actions])
    captured = max([observation.captured_at, action_coverage.captured_at] + [a.captured_at for a in used_actions])
    adjusted = replace(observation, context=context, volume=None if observation.volume is None else exact_multiply(observation.volume, ratio),
                       available_at=available, captured_at=captured, **prices)
    signature = numeric_signature(observation, ratio, tuple(used_actions), basis_session, action_coverage)
    return AdjustedPrice(adjusted, observation.id, factor, ids, action_coverage_ref=action_coverage.evidence_ref,
                          input_signature=signature, quantity_factor=ratio)


@dataclass(frozen=True, slots=True)
class BenchmarkPoint:
    session: date
    value: Decimal | None
    reason: str | None
    input_id: str
    action_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TotalReturnResult:
    points: tuple[BenchmarkPoint, ...]
    currency: str | None
    policy: str = "gross_ex_date_reinvestment_v1"
    reason: str | None = None
    action_coverage_ref: str | None = None
    numeric_policy: str = NUMERIC_POLICY
    input_signature: str | None = None


def total_return_benchmark(observations: Sequence[PriceObservation], actions: Iterable[CorporateAction], *,
                           as_of: datetime, history_mode: str = "reconstructed_public",
                           base_value: Decimal = Decimal(100),
                           decision_times: Mapping[str, datetime] | None = None,
                           action_coverage: ActionCoverage | None = None) -> TotalReturnResult:
    """Gross ex-date dividend reinvestment benchmark, independent of tradable prices.

    Cash dividends are reinvested at that session's close without tax/cost. This
    is a benchmark policy, not realized portfolio cash accounting. Unsupported
    events or incompatible cash/share bases make this derived series unavailable.
    The caller supplies a complete ordered calendar-aligned close sequence.
    ``action_coverage`` must certify the full first-to-last session interval for
    all relevant corporate-action types/revisions, including splits, dividends,
    unsupported complex events, and explicit no-event evidence. The same public
    and observed capture/adoption cutoff gates as split adjustment apply. Missing
    or incomplete coverage returns null benchmark points and the reason
    ``action_coverage_unavailable``; source prices are never changed.
    Only currency_per_share/share inputs are supported. Cash dividends apply
    on their ex date irrespective of a future payment/general effective date.
    Each benchmark point rounds once after exact numerator construction using
    the versioned price numeric policy, independent of the caller context.
    """
    if not base_value.is_finite() or base_value <= 0:
        raise ValueError("benchmark base must be positive")
    if not observations:
        return TotalReturnResult((), None)
    ordered = sorted(observations, key=lambda p: p.session)
    if len({p.session for p in ordered}) != len(ordered):
        raise ValueError("duplicate benchmark session")
    context = ordered[0].context
    for bar in ordered:
        normalize_observation(bar, requested_basis="as_traded")
        if bar.context != context:
            raise ValueError("benchmark inputs have incompatible context")
    action_rows = tuple(actions)
    if context.price_unit != "currency_per_share" or context.quantity_unit != "shares":
        unavailable = tuple(BenchmarkPoint(bar.session, None, "unsupported_benchmark_units", bar.id) for bar in ordered)
        return TotalReturnResult(unavailable, context.currency, reason="unsupported_benchmark_units")
    if not has_action_coverage(action_coverage, security_id=context.security_id,
                               from_session=ordered[0].session, through_session=ordered[-1].session,
                               as_of=as_of, history_mode=history_mode, actions=action_rows,
                               application_policy="gross_ex_date_reinvestment_v1"):
        unavailable = tuple(BenchmarkPoint(bar.session, None, "action_coverage_unavailable", bar.id) for bar in ordered)
        return TotalReturnResult(unavailable, context.currency, reason="action_coverage_unavailable")
    selected = select_visible_actions(action_rows, as_of=as_of, history_mode=history_mode, decision_times=decision_times,
                                      application_policy="gross_ex_date_reinvestment_v1")
    selected = tuple(a for a in selected if a.security_id == context.security_id)
    if len({a.event_id for a in selected}) != len(selected):
        raise ValueError("benchmark actions require a single selected source per event")
    points: list[BenchmarkPoint] = []
    index: Decimal | None = fixed_round(base_value)
    reason: str | None = None
    previous: PriceObservation | None = None
    for bar in ordered:
        if not visible_at(available_at=bar.available_at, captured_at=bar.captured_at, as_of=as_of,
                          history_mode=history_mode, decision_recorded_at=bar.decision_recorded_at):
            reason, index = "input_not_visible", None
        effective = [] if previous is None else [a for a in selected if previous.session < action_application_date(a, policy="gross_ex_date_reinvestment_v1") <= bar.session]
        ids = tuple(a.id for a in effective)
        if any(a.action_type not in {"split", "cash_dividend"} for a in effective):
            reason, index = "unsupported_action", None
        if any(a.action_type == "cash_dividend" and (a.currency != context.currency or a.cash_share_basis != "pre_event_share") for a in effective):
            reason, index = "incompatible_dividend_basis", None
        # Multiple split/cash combinations need explicitly ordered share bases.
        if any(a.action_type == "split" for a in effective) and any(a.action_type == "cash_dividend" for a in effective):
            reason, index = "unsupported_combined_action_basis", None
        if bar.close is None or bar.close <= 0 or (previous is not None and (previous.close is None or previous.close <= 0)):
            reason, index = "missing_or_zero_close", None
        if index is not None and previous is not None:
            ratio = Decimal(1)
            cash = Decimal(0)
            for event in effective:
                if event.action_type == "split":
                    ratio = exact_multiply(ratio, event.split_ratio)
                elif event.action_type == "cash_dividend":
                    cash = exact_sum((cash, event.cash_amount))
            value_before_division = exact_multiply(index, exact_sum((exact_multiply(bar.close, ratio), cash)))
            index = fixed_divide(value_before_division, previous.close)
        points.append(BenchmarkPoint(bar.session, index, reason, bar.id, ids))
        previous = bar
    return TotalReturnResult(tuple(points), context.currency,
                              reason=next((point.reason for point in points if point.reason), None),
                              action_coverage_ref=action_coverage.evidence_ref,
                              input_signature=numeric_signature(tuple(ordered), selected, base_value, action_coverage,
                                                                "gross_ex_date_reinvestment_v1"))
