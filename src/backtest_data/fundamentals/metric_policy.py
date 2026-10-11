"""Shared eligibility and result contracts for financial_metrics_v1."""

from datetime import datetime
from typing import Iterable

from .domain import FinancialFact, MetricResult, aware
from .selection import fact_visible
from .numeric import NUMERIC_POLICY

REQUIRED_GROWTH_METRICS = tuple(f'growth_{metric}_{kind}' for metric in
                              ('revenue', 'eps_basic', 'eps_diluted', 'operating_income')
                              for kind in ('qoq', 'yoy', 'ttm_yoy'))
OPTIONAL_GROWTH_METRICS = tuple(f'growth_{metric}_{kind}' for metric in
                              ('net_income_total', 'cfo', 'fcf') for kind in ('qoq', 'yoy', 'ttm_yoy'))
ANNUAL_GROWTH_METRICS = tuple(f'growth_{metric}_fy_yoy' for metric in
                            ('revenue', 'eps_basic', 'eps_diluted', 'operating_income'))
EPS_METRICS = {'eps_basic', 'eps_diluted'}


def input_ids(facts: Iterable[FinancialFact]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(record_id for fact in facts for record_id in fact.input_ids))


def result(metric_id: str, facts: Iterable[FinancialFact], *, value=None, reason: str | None = None,
           status: str | None = None, unit: str = 'ratio', materialized_at: datetime | None = None,
           **kwargs) -> MetricResult:
    facts = tuple(facts)
    available = [fact.available_at for fact in facts if fact.available_at is not None]
    kwargs['evidence']={**kwargs.get('evidence',{}),'numeric_policy':NUMERIC_POLICY}
    return MetricResult(metric_id, value, status or ('ok' if reason is None else 'unavailable'), reason, unit,
                        input_ids(facts), max(available) if available else None, materialized_at,
                        definition_version='financial_metrics_annual_v1' if metric_id.endswith('_fy_yoy') or metric_id == 'per_diluted_fy' else 'financial_metrics_v1',
                        scope=facts[0].scope if facts else None, currency=facts[0].currency if facts else None,
                        share_basis=facts[0].share_basis if facts else None, **kwargs)


def compatible_facts(facts: Iterable[FinancialFact], *, as_of: datetime,
                     history_mode: str = 'reconstructed_public', check_unit: bool = True,
                     check_currency: bool = True,
                     check_scope: bool = True, same_period: bool = False,
                     same_cycle: bool = False) -> str | None:
    facts = tuple(facts)
    if not facts or any(fact.value is None for fact in facts):
        return 'missing_input'
    if any(not fact_visible(fact, as_of=as_of, history_mode=history_mode) for fact in facts):
        return 'future_or_unobserved_input'
    if any(not fact.period_compatible for fact in facts):
        return 'incompatible_period'
    if len({fact.release_id for fact in facts}) > 1:
        return 'release_mismatch'
    if any(fact.fiscal_lineage is None for fact in facts):
        return 'missing_fiscal_lineage'
    if len({fact.fiscal_lineage for fact in facts}) > 1:
        return 'fiscal_lineage_mismatch'
    if same_cycle and (any(fact.fiscal_cycle is None for fact in facts) or len({fact.fiscal_cycle for fact in facts}) > 1):
        return 'fiscal_cycle_mismatch'
    if any(fact.currency is None for fact in facts):
        return 'missing_currency'
    if check_currency and len({fact.currency for fact in facts}) > 1:
        return 'currency_mismatch'
    if check_unit and len({fact.unit for fact in facts}) > 1:
        return 'unit_mismatch'
    if check_scope and len({fact.scope for fact in facts}) > 1:
        return 'scope_mismatch'
    if any(fact.scope == 'unknown' for fact in facts):
        return 'unknown_scope'
    if any(fact.accounting_basis is None for fact in facts):
        return 'missing_accounting_basis'
    if len({fact.accounting_basis for fact in facts}) > 1:
        return 'accounting_basis_mismatch'
    if len({fact.dimensions for fact in facts}) > 1:
        return 'context_mismatch'
    if any(fact.metric in EPS_METRICS for fact in facts):
        eps_facts=tuple(fact for fact in facts if fact.metric in EPS_METRICS)
        if any(fact.unit not in {f'{fact.currency}/share',f'{fact.currency}/shares'} for fact in eps_facts):
            return 'per_share_unit_required'
        if any(fact.scope!='parent_common' for fact in eps_facts):
            return 'common_eps_scope_required'
        if any(fact.share_basis is None for fact in facts):
            return 'missing_share_basis'
        if len({fact.share_basis for fact in facts}) > 1:
            return 'share_basis_mismatch'
    if same_period and len({(fact.period_kind, fact.period_start, fact.period_end) for fact in facts}) > 1:
        return 'period_mismatch'
    return None


def materialization_reason(history_mode: str, as_of: datetime, materialized_at: datetime | None) -> str | None:
    aware(as_of, 'as_of')
    aware(materialized_at, 'materialized_at')
    if history_mode not in {'reconstructed_public', 'observed_system'}:
        raise ValueError(f'unsupported_history_mode:{history_mode}')
    if history_mode == 'observed_system' and (materialized_at is None or materialized_at > as_of):
        return 'materialization_after_cutoff'
    return None
