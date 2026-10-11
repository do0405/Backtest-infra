"""Explicit PIT split/ADR rebasing, with action evidence in the input closure."""

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from .domain import FinancialFact, aware, stable_hash
from .metric_policy import EPS_METRICS
from .numeric import NUMERIC_POLICY, deterministic_decimal, exact_multiply


@dataclass(frozen=True, slots=True)
class ShareBasisAdjustment:
    id: str
    from_basis: str
    to_basis: str
    shares_multiplier: Decimal
    available_at: datetime
    captured_at: datetime
    verified: bool = False
    decision_recorded_at: datetime | None = None
    definition_version: str = 'share_basis_v1'

    def __post_init__(self):
        for name in ('available_at','captured_at','decision_recorded_at'):
            aware(getattr(self,name),name)
        if not isinstance(self.shares_multiplier,Decimal) or not self.shares_multiplier.is_finite() or self.shares_multiplier<=0:
            raise ValueError('positive_finite_shares_multiplier_required')


@deterministic_decimal
def _rebase(fact: FinancialFact, adjustment: ShareBasisAdjustment, *, as_of: datetime,
             history_mode: str, inverse: bool) -> FinancialFact:
    aware(as_of,'as_of')
    if history_mode not in {'reconstructed_public','observed_system'}:
        raise ValueError(f'unsupported_history_mode:{history_mode}')
    if (not adjustment.verified or adjustment.available_at>as_of or
        history_mode=='observed_system' and (adjustment.captured_at>as_of or
                                             adjustment.decision_recorded_at is None or adjustment.decision_recorded_at>as_of)):
        raise ValueError('unverified_or_future_share_basis')
    if fact.share_basis!=adjustment.from_basis or fact.value is None:
        raise ValueError('share_basis_mismatch')
    value=fact.value/adjustment.shares_multiplier if inverse else exact_multiply(fact.value,adjustment.shares_multiplier)
    # A later action adoption is part of the same observation closure. Unknown
    # adoption on either input must remain unknown rather than borrowing the
    # original source fact's earlier decision.
    decision_recorded_at=(max(fact.decision_recorded_at,adjustment.decision_recorded_at)
                          if fact.decision_recorded_at is not None and adjustment.decision_recorded_at is not None else None)
    return replace(fact,id=stable_hash((fact.id,adjustment.id,adjustment.to_basis,NUMERIC_POLICY)),value=value,
                   share_basis=adjustment.to_basis,source_record_ids=fact.input_ids+(adjustment.id,),
                   available_at=max(fact.available_at,adjustment.available_at) if fact.available_at else None,
                   captured_at=max(fact.captured_at,adjustment.captured_at),
                   decision_recorded_at=decision_recorded_at,
                   evidence={**fact.evidence,'share_basis_adjustment':adjustment.id,
                             'share_basis_definition':adjustment.definition_version,'shares_multiplier':str(adjustment.shares_multiplier),
                             'numeric_policy':NUMERIC_POLICY,'share_basis_action_available_at':adjustment.available_at.isoformat(),
                             'share_basis_action_captured_at':adjustment.captured_at.isoformat(),
                             'share_basis_action_decision_recorded_at':adjustment.decision_recorded_at.isoformat() if adjustment.decision_recorded_at else None})


def rebase_eps(fact: FinancialFact, adjustment: ShareBasisAdjustment, *, as_of: datetime,
                history_mode: str = 'reconstructed_public') -> FinancialFact:
    if fact.metric not in EPS_METRICS:
        raise ValueError('reported_eps_required')
    return _rebase(fact,adjustment,as_of=as_of,history_mode=history_mode,inverse=True)


def rebase_outstanding_shares(fact: FinancialFact, adjustment: ShareBasisAdjustment, *, as_of: datetime,
                              history_mode: str = 'reconstructed_public') -> FinancialFact:
    if fact.metric!='shares_outstanding_common':
        raise ValueError('pit_outstanding_shares_required')
    return _rebase(fact,adjustment,as_of=as_of,history_mode=history_mode,inverse=False)
