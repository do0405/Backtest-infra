"""Compatible reported-period arithmetic with complete input lineage."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable

from .domain import FinancialFact, MetricResult
from .metric_policy import EPS_METRICS, compatible_facts, materialization_reason, result
from .numeric import deterministic_decimal, exact_difference, exact_sum


@deterministic_decimal
def quarter_from_ytd(current: FinancialFact, previous: FinancialFact, *, as_of: datetime,
                      history_mode: str = 'reconstructed_public', materialized_at: datetime | None = None) -> MetricResult:
    facts = (current, previous)
    reason = materialization_reason(history_mode, as_of, materialized_at)
    if current.metric in EPS_METRICS or previous.metric in EPS_METRICS:
        reason = reason or 'eps_requires_reported_quarters'
    reason = reason or compatible_facts(facts, as_of=as_of, history_mode=history_mode, same_cycle=True)
    if reason is None:
        if current.metric != previous.metric:
            reason = 'metric_mismatch'
        elif current.period_kind not in {'ytd', 'annual'} or previous.period_kind not in {'ytd', 'quarter'}:
            reason = 'ytd_period_required'
        elif current.period_start != previous.period_start or current.period_end <= previous.period_end:
            reason = 'period_mismatch'
        elif current.fiscal_quarter is None or previous.fiscal_quarter != current.fiscal_quarter - 1:
            reason = 'missing_previous_ytd'
    if reason:
        return result(current.metric, facts, reason=reason, unit=current.unit, materialized_at=materialized_at)
    return result(current.metric, facts, value=exact_difference(current.value,previous.value), unit=current.unit,
                  materialized_at=materialized_at, evidence={'period_kind': 'quarter',
                   'period_start': previous.period_end+timedelta(days=1), 'period_end': current.period_end,
                   'directly_reported': False, 'method': 'compatible_ytd_difference'})


@deterministic_decimal
def sum_quarters(facts: Iterable[FinancialFact], *, as_of: datetime,
                  history_mode: str = 'reconstructed_public', materialized_at: datetime | None = None,
                  metric_id: str | None = None) -> MetricResult:
    facts = tuple(sorted(facts, key=lambda fact: fact.period_end))
    metric_id = metric_id or (facts[0].metric + '_ttm' if facts else 'unknown_ttm')
    reason = materialization_reason(history_mode, as_of, materialized_at)
    if len(facts) != 4 or any(fact.period_kind != 'quarter' for fact in facts):
        reason = reason or 'four_compatible_quarters_required'
    reason = reason or compatible_facts(facts, as_of=as_of, history_mode=history_mode)
    if reason is None:
        if len({fact.metric for fact in facts}) != 1:
            reason = 'metric_mismatch'
        elif any(fact.quarter_index is None for fact in facts) or any(b.quarter_index != a.quarter_index+1 for a,b in zip(facts,facts[1:])):
            reason = 'nonconsecutive_quarters'
        elif any(fact.period_start is None for fact in facts) or any(b.period_start != a.period_end+timedelta(days=1) for a,b in zip(facts,facts[1:])):
            reason = 'nonconsecutive_periods'
        elif facts[0].metric in EPS_METRICS and any(not fact.directly_reported for fact in facts):
            reason = 'eps_requires_reported_quarters'
    if reason:
        return result(metric_id, facts, reason=reason, unit=facts[0].unit if facts else 'unknown', materialized_at=materialized_at)
    evidence = {'period_kind':'ttm', 'period_start':facts[0].period_start, 'period_end':facts[-1].period_end,
                'quarter_indices':tuple(fact.quarter_index for fact in facts)}
    if facts[0].metric in EPS_METRICS:
        evidence['eps_method'] = f'sum_reported_{facts[0].metric.removeprefix("eps_")}_quarters'
        evidence['input_precision'] = tuple(fact.evidence.get('source_decimals') for fact in facts)
    return result(metric_id, facts, value=exact_sum(fact.value for fact in facts),
                  unit=facts[0].unit, materialized_at=materialized_at, evidence=evidence)
