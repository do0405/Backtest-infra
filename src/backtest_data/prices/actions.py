"""Corporate-action revisions and conservative point-in-time visibility."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from .domain import finite_decimal, require_aware, visible_at


@dataclass(frozen=True, slots=True)
class ActionCoverage:
    """Verified source-ledger coverage, including explicit no-event intervals.

    The evidence must certify all relevant corporate-action types/revisions for
    this security and inclusive economic interval under the requested history
    mode. A nonempty event list, missing row, or latest-only archive is not such
    evidence. ``verified`` is a source-contract adjudication, never inferred.
    Public and capture timestamps describe the coverage evidence itself.
    """

    security_id: str
    from_session: date
    through_session: date
    source_contract_id: str
    evidence_ref: str
    available_at: datetime | None
    captured_at: datetime
    verified: bool = False
    decision_recorded_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.security_id or not self.source_contract_id or not self.evidence_ref:
            raise ValueError("action coverage identity and evidence must be explicit")
        if self.from_session > self.through_session:
            raise ValueError("action coverage interval is reversed")
        for name in ("available_at", "captured_at", "decision_recorded_at"):
            require_aware(getattr(self, name), name)


def has_action_coverage(coverage: ActionCoverage | None, *, security_id: str, from_session: date,
                        through_session: date, as_of: datetime, history_mode: str,
                        actions: Iterable["CorporateAction"] = (),
                        application_policy: str = "effective_date") -> bool:
    """Require ledger coverage using the derivative's economic application dates.

    Gross ex-date reinvestment checks dividend ex dates even if payment/general
    effective dates are later. Splits continue to use their effective dates.
    """
    _validate_application_policy(application_policy)
    if coverage is None or not coverage.verified or coverage.security_id != security_id:
        return False
    if coverage.from_session > from_session or coverage.through_session < through_session:
        return False
    if not visible_at(available_at=coverage.available_at, captured_at=coverage.captured_at,
                      as_of=as_of, history_mode=history_mode, decision_recorded_at=coverage.decision_recorded_at):
        return False
    action_rows = tuple(actions)
    relevant_events = {action.event_id for action in action_rows if action.security_id == security_id
                       and from_session < action_application_date(action, policy=application_policy) <= through_session}
    for action in action_rows:
        if action.security_id == security_id and action.event_id in relevant_events:
            if action.available_at is None:
                return False
            if action.available_at <= as_of and action.source_contract_id != coverage.source_contract_id:
                return False
    return True


def _validate_application_policy(policy: str) -> None:
    if policy not in {"effective_date", "gross_ex_date_reinvestment_v1"}:
        raise ValueError("unsupported corporate-action application policy")


def action_application_date(action: "CorporateAction", *, policy: str = "effective_date") -> date:
    """Economic date for a versioned derivative policy, independent of availability."""
    _validate_application_policy(policy)
    if policy == "gross_ex_date_reinvestment_v1" and action.action_type == "cash_dividend" and action.ex_date is not None:
        return action.ex_date
    return action.effective_date


@dataclass(frozen=True, slots=True)
class CorporateAction:
    """Immutable source revision; ``split_ratio`` is new shares / old shares.

    Unknown action types remain evidence and are deliberately not coerced into a
    simple split. Cash is per share on the declared ``cash_share_basis``; complex
    combinations need a separately verified policy.
    """

    id: str
    event_id: str
    security_id: str
    action_type: str
    revision: int
    available_at: datetime | None
    captured_at: datetime
    effective_date: date
    ex_date: date | None
    source_contract_id: str
    raw_ref: str
    split_ratio: Decimal | None = None
    cash_amount: Decimal | None = None
    currency: str | None = None
    received_security_id: str | None = None
    announced_at: datetime | None = None
    pay_date: date | None = None
    decision_recorded_at: datetime | None = None
    cash_share_basis: str | None = None
    cancelled: bool = False

    def __post_init__(self) -> None:
        for name in ("available_at", "captured_at", "announced_at", "decision_recorded_at"):
            require_aware(getattr(self, name), name)
        if not all((self.id, self.event_id, self.security_id, self.action_type, self.source_contract_id, self.raw_ref)):
            raise ValueError("action identity and provenance must be explicit")
        if self.revision < 1:
            raise ValueError("action revision must be positive")
        finite_decimal(self.split_ratio, "split_ratio", positive=True)
        finite_decimal(self.cash_amount, "cash_amount")
        if self.action_type == "split" and self.split_ratio is None and not self.cancelled:
            raise ValueError("split requires a ratio")
        if self.action_type == "cash_dividend" and not self.cancelled:
            if self.cash_amount is None or not self.currency or self.ex_date is None:
                raise ValueError("cash dividend requires amount, currency, and ex_date")


def select_visible_actions(actions: Iterable[CorporateAction], *, as_of: datetime,
                           history_mode: str = "reconstructed_public",
                           decision_times: Mapping[str, datetime] | None = None,
                           effective_by: date | None = None,
                           application_policy: str = "effective_date") -> tuple[CorporateAction, ...]:
    """Select the latest visible revision per source event, then apply effectiveness.

    A later revision which moves an event into the future/cancels it supersedes
    the prior revision; filtering effectiveness before revision selection would
    incorrectly resurrect the old action.
    ``gross_ex_date_reinvestment_v1`` uses cash-dividend ex dates for economic
    effectiveness; publication/capture/decision gates still apply separately.
    """
    require_aware(as_of, "as_of")
    _validate_application_policy(application_policy)
    cutoff = min(effective_by or as_of.date(), as_of.date())
    grouped: dict[tuple[str, str, str], list[CorporateAction]] = {}
    seen: dict[str, CorporateAction] = {}
    for action in actions:
        if action.id in seen and seen[action.id] != action:
            raise ValueError("conflicting action identity")
        seen[action.id] = action
        decision = (decision_times or {}).get(action.id, action.decision_recorded_at)
        if visible_at(available_at=action.available_at, captured_at=action.captured_at,
                      as_of=as_of, history_mode=history_mode, decision_recorded_at=decision):
            grouped.setdefault((action.security_id, action.source_contract_id, action.event_id), []).append(action)
    selected: list[CorporateAction] = []
    for values in grouped.values():
        by_revision: dict[int, CorporateAction] = {}
        for action in values:
            previous = by_revision.get(action.revision)
            if previous is not None and previous != action:
                raise ValueError("conflicting corporate-action revision")
            by_revision[action.revision] = action
        action = max(by_revision.values(), key=lambda a: a.revision)
        if action_application_date(action, policy=application_policy) <= cutoff and not action.cancelled:
            selected.append(action)
    return tuple(sorted(selected, key=lambda a: (action_application_date(a, policy=application_policy), a.event_id, a.id)))
