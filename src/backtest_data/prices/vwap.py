"""Exact daily, calendar-window, and anchored VWAP from matched statistics."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from .actions import ActionCoverage, CorporateAction, has_action_coverage, select_visible_actions
from .adjustments import split_quantity_factor
from .domain import HistoryMode, PriceContext, finite_decimal, require_aware, visible_at
from .numeric import NUMERIC_POLICY, exact_multiply, exact_sum, fixed_divide, numeric_signature


EXACT_METHODS = frozenset({"matched_daily_statistics", "trade_aggregated"})


@dataclass(frozen=True, slots=True)
class DailyVWAPStat:
    """Matched N=sum(price*quantity) and D=sum(eligible quantity), one revision.

    General provider bar volume cannot be substituted for D. ``trade_scope``
    must identify the approved session/condition eligibility rule. N and D use
    ``context``'s currency and quantity units, including its share basis.
    """

    id: str
    context: PriceContext
    session: date
    sum_price_volume: Decimal | None
    eligible_volume: Decimal | None
    trade_scope: str
    method: str
    source_contract_id: str
    raw_ref: str
    revision: int
    available_at: datetime | None
    captured_at: datetime
    materialized_at: datetime | None
    decision_recorded_at: datetime | None = None
    no_trade_evidence: str | None = None
    historical_replay_supported: bool = True
    input_event_ids: tuple[str, ...] = ()
    incomplete_reason: str | None = None
    numeric_policy: str = NUMERIC_POLICY
    input_signature: str | None = None

    def __post_init__(self) -> None:
        for name in ("available_at", "captured_at", "materialized_at", "decision_recorded_at"):
            require_aware(getattr(self, name), name)
        finite_decimal(self.sum_price_volume, "sum_price_volume")
        finite_decimal(self.eligible_volume, "eligible_volume")
        if self.eligible_volume == 0:
            if not self.no_trade_evidence:
                raise ValueError("zero eligible_volume requires no-trade evidence")
            if self.sum_price_volume not in (None, Decimal(0)):
                raise ValueError("zero eligible volume cannot have positive numerator")
        if not all((self.id, self.trade_scope, self.source_contract_id, self.raw_ref)) or self.revision < 1:
            raise ValueError("statistics identity, scope, provenance, and revision must be explicit")


@dataclass(frozen=True, slots=True)
class Anchor:
    session: date
    known_at: datetime
    rule: str

    def __post_init__(self) -> None:
        require_aware(self.known_at, "anchor_known_at")
        if not self.rule:
            raise ValueError("anchor_rule must be explicit")


@dataclass(frozen=True, slots=True)
class VWAPResult:
    value: Decimal | None
    sum_price_volume: Decimal | None
    eligible_volume: Decimal | None
    sessions: tuple[date, ...]
    input_ids: tuple[str, ...]
    action_ids: tuple[str, ...] = ()
    missing_sessions: tuple[date, ...] = ()
    reason: str | None = None
    basis_session: date | None = None
    query_rule: str = "exact_vwap_v1"
    action_coverage_ref: str | None = None
    numeric_policy: str = NUMERIC_POLICY
    input_signature: str | None = None


def _session_grid(sessions: Sequence[date]) -> tuple[date, ...]:
    grid = tuple(sessions)
    if not grid or tuple(sorted(set(grid))) != grid:
        raise ValueError("session grid must be nonempty, ordered, and unique")
    return grid


def _unavailable(grid: tuple[date, ...], reason: str, *, missing: tuple[date, ...] = (),
                 ids: tuple[str, ...] = (), basis_session: date | None = None) -> VWAPResult:
    return VWAPResult(None, None, None, grid, ids, missing_sessions=missing,
                      reason=reason, basis_session=basis_session)


def exact_vwap(statistics: Iterable[DailyVWAPStat], *, sessions: Sequence[date], as_of: datetime,
               history_mode: str = "reconstructed_public", basis_session: date | None = None,
               actions: Iterable[CorporateAction] = (),
               action_decision_times: Mapping[str, datetime] | None = None,
               compatible_source_contracts: frozenset[str] | None = None,
               action_coverage: ActionCoverage | None = None) -> VWAPResult:
    """Calculate sum(N)/sum(D) over an explicit completed-session grid.

    All requested sessions must have a visible selected revision. Missing and
    incompatible inputs return a reason, without fallback to provider VWAP or
    bar proxies. Cross-source mixing requires an explicit compatibility set;
    multiple observations of one session still require upstream selection.
    Raw N is preserved while each D is mapped to the query share basis.
    Cross-session aggregation, an explicit ``basis_session``, or adjusted input
    requires verified ``ActionCoverage`` across the whole query interval. A raw
    single-session statistic queried without ``basis_session`` stays usable
    without action coverage because it performs no share-basis transformation.
    """
    grid = _session_grid(sessions)
    require_aware(as_of, "as_of")
    mode = HistoryMode(history_mode)
    basis = basis_session or grid[-1]
    if grid[-1] > as_of.date() or basis < grid[-1] or basis > as_of.date():
        raise ValueError("VWAP sessions and basis must not exceed cutoff")
    rows: dict[tuple[str, str, date, str], list[DailyVWAPStat]] = {}
    identities: dict[str, DailyVWAPStat] = {}
    for stat in statistics:
        if stat.id in identities and identities[stat.id] != stat:
            raise ValueError("conflicting statistic identity")
        identities[stat.id] = stat
        if stat.session not in grid:
            continue
        if not visible_at(available_at=stat.available_at, captured_at=stat.captured_at,
                          as_of=as_of, history_mode=mode, decision_recorded_at=stat.decision_recorded_at,
                          materialized_at=stat.materialized_at, require_materialized=True):
            continue
        key = (stat.context.security_id, stat.source_contract_id, stat.session, stat.trade_scope)
        rows.setdefault(key, []).append(stat)
    by_session: dict[date, list[DailyVWAPStat]] = {}
    for candidates in rows.values():
        revisions: dict[int, DailyVWAPStat] = {}
        for candidate in candidates:
            previous = revisions.get(candidate.revision)
            if previous is not None and previous != candidate:
                raise ValueError("conflicting statistic revision")
            revisions[candidate.revision] = candidate
        selected = revisions[max(revisions)]
        by_session.setdefault(selected.session, []).append(selected)
    missing = tuple(session for session in grid if session not in by_session)
    if missing:
        return _unavailable(grid, "missing_sessions", missing=missing, basis_session=basis)
    if any(len(by_session[session]) != 1 for session in grid):
        return _unavailable(grid, "ambiguous_session_selection", basis_session=basis)
    selected = [by_session[session][0] for session in grid]
    ids = tuple(s.id for s in selected)
    if any(s.method not in EXACT_METHODS for s in selected):
        return _unavailable(grid, "method_not_exact", ids=ids, basis_session=basis)
    if any(s.numeric_policy != NUMERIC_POLICY for s in selected):
        return _unavailable(grid, "unsupported_numeric_policy", ids=ids, basis_session=basis)
    if mode == HistoryMode.OBSERVED_SYSTEM and any(not s.historical_replay_supported for s in selected):
        return _unavailable(grid, "historical_replay_unsupported", ids=ids, basis_session=basis)
    first = selected[0]
    contracts = {s.source_contract_id for s in selected}
    explicit_context = all(getattr(first.context, field) for field in
                           ("security_id", "listing_id", "venue", "feed", "session_scope", "currency", "price_unit", "quantity_unit", "basis"))
    context_ok = (explicit_context and first.context.basis in {"as_traded", "split_adjusted"}
                  and first.context.quantity_unit == "shares"
                  and first.context.price_unit == "currency_per_share"
                  and all(s.context == first.context and s.trade_scope == first.trade_scope for s in selected))
    source_ok = len(contracts) == 1 or (compatible_source_contracts is not None and contracts <= compatible_source_contracts)
    if not context_ok or not source_ok:
        return _unavailable(grid, "incompatible_statistics", ids=ids, basis_session=basis)
    if first.context.basis == "split_adjusted" and first.context.share_basis != f"as_of:{basis.isoformat()}":
        return _unavailable(grid, "incompatible_statistics", ids=ids, basis_session=basis)
    if any(s.sum_price_volume is None or s.eligible_volume is None for s in selected):
        reason = next((s.incomplete_reason for s in selected if s.incomplete_reason), "missing_statistics")
        return _unavailable(grid, reason, ids=ids, basis_session=basis)
    action_rows = tuple(actions)
    adjusts_splits = len(grid) > 1 or basis_session is not None or first.context.basis == "split_adjusted"
    if adjusts_splits and not has_action_coverage(action_coverage, security_id=first.context.security_id,
                                                 from_session=grid[0], through_session=basis,
                                                 as_of=as_of, history_mode=mode, actions=action_rows):
        return _unavailable(grid, "action_coverage_unavailable", ids=ids, basis_session=basis)
    visible_actions = select_visible_actions(action_rows, as_of=as_of, history_mode=mode,
                                              decision_times=action_decision_times, effective_by=basis)
    if any(a.security_id == first.context.security_id and grid[0] < a.effective_date <= basis
           and a.action_type not in {"split", "cash_dividend"} for a in visible_actions):
        return _unavailable(grid, "unsupported_action", ids=ids, basis_session=basis)
    numerator_terms: list[Decimal] = []
    denominator_terms: list[Decimal] = []
    action_ids: list[str] = []
    for stat in selected:
        ratio, used = (Decimal(1), ()) if stat.context.basis == "split_adjusted" or not adjusts_splits else split_quantity_factor(
            security_id=stat.context.security_id, session=stat.session, basis_session=basis,
            actions=action_rows, as_of=as_of, history_mode=mode, decision_times=action_decision_times, action_coverage=action_coverage)
        numerator_terms.append(stat.sum_price_volume)
        denominator_terms.append(exact_multiply(stat.eligible_volume, ratio))
        action_ids.extend(used)
    numerator, denominator = exact_sum(numerator_terms), exact_sum(denominator_terms)
    ids_actions = tuple(dict.fromkeys(action_ids))
    value = None if denominator == 0 else fixed_divide(numerator, denominator)
    return VWAPResult(value, numerator, denominator, grid, ids, ids_actions,
                      reason="zero_eligible_volume" if denominator == 0 else None, basis_session=basis,
                      action_coverage_ref=action_coverage.evidence_ref if adjusts_splits else None,
                      input_signature=numeric_signature(tuple(selected), grid, basis, ids_actions, action_coverage,
                                                        numerator, denominator, "exact_vwap_v1"))


def daily_vwap(statistics: Iterable[DailyVWAPStat], *, session: date, **query) -> VWAPResult:
    """Exact single-session VWAP with the same visibility/compatibility gates."""
    return exact_vwap(statistics, sessions=[session], **query)


def rolling_vwap(statistics: Iterable[DailyVWAPStat], *, session_grid: Sequence[date],
                 end_session: date, window: int, **query) -> VWAPResult:
    """Use N exchange sessions ending at ``end_session``, never N observed rows."""
    grid = _session_grid(session_grid)
    if window < 1:
        raise ValueError("rolling window must be positive")
    if end_session not in grid:
        raise ValueError("end_session is absent from exchange session grid")
    end = grid.index(end_session) + 1
    if end < window:
        return _unavailable(grid[:end], "insufficient_session_grid")
    return exact_vwap(statistics, sessions=grid[end-window:end], **query)


def anchored_vwap(statistics: Iterable[DailyVWAPStat], *, session_grid: Sequence[date],
                  end_session: date, anchor: Anchor, as_of: datetime, **query) -> VWAPResult:
    """Require the anchor's historical knowledge time as well as its session."""
    grid = _session_grid(session_grid)
    require_aware(as_of, "as_of")
    if anchor.known_at > as_of:
        return _unavailable(grid, "anchor_not_known")
    if anchor.session not in grid or end_session not in grid or anchor.session > end_session:
        return _unavailable(grid, "anchor_outside_session_grid")
    return exact_vwap(statistics, sessions=grid[grid.index(anchor.session):grid.index(end_session)+1], as_of=as_of, **query)
