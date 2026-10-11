"""Accounting checks use reported numeric uncertainty, never a guessed epsilon."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Iterable

from .domain import FinancialFact
from .metric_policy import compatible_facts, input_ids
from .numeric import NUMERIC_POLICY, deterministic_decimal, exact_difference, exact_multiply, exact_sum


@dataclass(frozen=True, slots=True)
class AccountingValidation:
    status: str
    reason: str | None
    difference: Decimal | None
    tolerance: Decimal | None
    input_record_ids: tuple[str, ...]
    numeric_policy: str = NUMERIC_POLICY


@deterministic_decimal
def reported_uncertainty(fact: FinancialFact) -> Decimal | None:
    decimals=fact.evidence.get('source_decimals')
    precision=fact.evidence.get('source_precision')
    if decimals=='INF' or precision=='INF':
        return Decimal(0)
    try:
        multiplier=Decimal(str(fact.evidence.get('normalization_multiplier','1')))
        source_value=Decimal(str(fact.evidence.get('source_value',fact.value))) if fact.value is not None else None
        if not multiplier.is_finite() or source_value is not None and not source_value.is_finite():
            return None
        if decimals is not None:
            exponent=-int(str(decimals))
        elif precision is not None and source_value is not None and source_value!=0:
            digits=int(str(precision))
            if digits<=0:
                return None
            exponent=source_value.adjusted()-digits+1
        else:
            return None
        if abs(exponent)>1000:
            return None
        with localcontext() as context:
            context.prec=50
            return exact_multiply(Decimal((0,(5,),exponent-1)),multiplier.copy_abs())
    except (ValueError,TypeError,InvalidOperation):
        return None


@deterministic_decimal
def check_accounting_identity(total: FinancialFact, components: Iterable[FinancialFact], *, as_of: datetime,
                                history_mode: str = 'reconstructed_public') -> AccountingValidation:
    components=tuple(components)
    facts=(total,)+components
    reason=compatible_facts(facts,as_of=as_of,history_mode=history_mode,same_period=True)
    if not components:
        reason=reason or 'missing_components'
    if len({fact.id for fact in facts})!=len(facts):
        reason=reason or 'duplicate_component'
    uncertainty=tuple(reported_uncertainty(fact) for fact in facts)
    if any(value is None for value in uncertainty):
        reason=reason or 'missing_reported_precision'
    if reason:
        return AccountingValidation('unavailable',reason,None,None,input_ids(facts))
    difference=exact_difference(total.value,exact_sum(fact.value for fact in components))
    tolerance=exact_sum(uncertainty)
    passed=difference.copy_abs()<=tolerance
    return AccountingValidation('passed' if passed else 'failed',
                                None if passed else 'accounting_identity_mismatch',
                                difference,tolerance,input_ids(facts))
