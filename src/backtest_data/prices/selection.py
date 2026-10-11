"""Auditable explicit price adoption; comparisons do not choose by majority."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from .domain import PriceObservation, require_aware
from .validation import ComparisonResult, validate_observation


@dataclass(frozen=True, slots=True)
class PriceSelectionDecision:
    id: str
    selected_observation_id: str | None
    observation_ids: tuple[str, ...]
    validation_issue_ids: tuple[str, ...]
    rule_version: str
    reason: str
    evidence_grade: str | None
    recorded_at: datetime
    status: str


def select_observation(observation: PriceObservation, *, source_contract_verified: bool,
                       recorded_at: datetime, rule_version: str, decision_id: str,
                       comparisons: Iterable[ComparisonResult] = (),
                       validation_issue_ids: Iterable[str] = (),
                       require_volume: bool = True) -> PriceSelectionDecision:
    """Record a caller's explicit candidate decision with evidence grade and IDs.

    Structural failure rejects the candidate. Unresolved equivalent-source
    comparisons quarantine it. Incomparable feeds are diagnostics, not false
    mismatches. A single contract can be accepted as contract_verified; it is not
    labelled independently crosschecked. Source values are never overwritten.
    """
    require_aware(recorded_at, "recorded_at")
    if not decision_id or not rule_version:
        raise ValueError("decision ID and rule version are required")
    if recorded_at < observation.captured_at or (observation.available_at is not None and recorded_at < observation.available_at):
        raise ValueError("selection cannot precede capture/availability")
    results = tuple(comparisons)
    if any(observation.id not in comparison.observation_ids for comparison in results):
        raise ValueError("comparison does not include selection candidate")
    ids = tuple(dict.fromkeys([observation.id] + [id for c in results for id in c.observation_ids]))
    issue_ids = tuple(dict.fromkeys(validation_issue_ids))
    structural = validate_observation(observation)
    if structural:
        status, reason, grade = "rejected", "structural_validation_failed", None
    elif observation.available_at is None:
        status, reason, grade = "quarantined", "availability_unknown", None
    elif require_volume and observation.volume is None:
        status, reason, grade = "quarantined", "required_quantity_missing", None
    elif any(c.status in {"mismatch", "unresolved"} for c in results):
        status, reason, grade = "quarantined", "unresolved_source_comparison", None
    elif not source_contract_verified:
        status, reason, grade = "provisional", "source_contract_not_verified", None
    elif any(c.status == "agree" and c.independent for c in results):
        status, reason, grade = "accepted", "verified_independent_comparison", "independent_crosschecked"
    else:
        status, reason, grade = "accepted", "verified_source_contract", "contract_verified"
    return PriceSelectionDecision(decision_id, observation.id if status == "accepted" else None,
                                   ids, issue_ids, rule_version, reason, grade, recorded_at, status)
