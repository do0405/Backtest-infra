"""Decimal calculations for the explicit financial metric catalog."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable

from .derive import sum_quarters
from .domain import FinancialFact, MetricResult
from .metric_policy import (ANNUAL_GROWTH_METRICS, EPS_METRICS, OPTIONAL_GROWTH_METRICS,
                            REQUIRED_GROWTH_METRICS, compatible_facts, materialization_reason, result)
from .selection import select_facts_asof
from .numeric import deterministic_decimal, exact_difference, exact_multiply, exact_sum


def _period_facts(facts, metric, index):
    return tuple(fact for fact in facts if fact.metric == metric and fact.period_kind == 'quarter' and fact.quarter_index == index)


def _window(facts, metric, end_index):
    selected = []
    for index in range(end_index-3, end_index+1):
        matches = _period_facts(facts, metric, index)
        if len(matches) != 1:
            return tuple(selected + list(matches)), 'ambiguous_input' if matches else 'four_compatible_quarters_required'
        selected.append(matches[0])
    return tuple(selected), None


def _latest_quarter(facts, metric):
    matches = [fact for fact in facts if fact.metric == metric and fact.period_kind == 'quarter' and fact.quarter_index is not None]
    if not matches:
        return None, 'missing_quarterly_input'
    latest = max(fact.quarter_index for fact in matches)
    matches = [fact for fact in matches if fact.quarter_index == latest]
    return (matches[0], None) if len(matches) == 1 else (None, 'ambiguous_input')


def _overlap(facts):
    seen = set()
    for fact in facts:
        components = set(fact.component_ids) or {fact.metric}
        if seen & components:
            return True
        seen.update(components)
    return False


@deterministic_decimal
def free_cash_flow(cfo: FinancialFact, capex: Iterable[FinancialFact], *, as_of: datetime,
                   history_mode: str = 'reconstructed_public', materialized_at: datetime | None = None) -> MetricResult:
    capex = tuple(fact for fact in capex if fact.metric in {'cash_capex','cash_capex_ppe','cash_capex_intangible'})
    facts = (cfo,) + capex
    metric_id = 'fcf_cfo_less_capex_q' if cfo.period_kind == 'quarter' else 'fcf_cfo_less_capex_ttm'
    reason = materialization_reason(history_mode, as_of, materialized_at)
    if cfo.metric != 'cfo':
        reason = reason or 'cfo_required'
    totals = [fact for fact in capex if fact.metric == 'cash_capex']
    details = [fact for fact in capex if fact.metric != 'cash_capex']
    if totals and details or _overlap(capex):
        reason = reason or 'overlapping_components'
    elif not (len(totals) == 1 or len(details) == 2 and {fact.metric for fact in details} == {'cash_capex_ppe','cash_capex_intangible'}):
        reason = reason or 'missing_cash_capex'
    reason = reason or compatible_facts(facts, as_of=as_of, history_mode=history_mode, same_period=True)
    uses = []
    if reason is None:
        for fact in capex:
            if fact.sign_policy == 'positive_outflow' and fact.value >= 0:
                uses.append(fact.value)
            elif fact.sign_policy == 'negative_outflow' and fact.value <= 0:
                uses.append(fact.value.copy_negate())
            else:
                reason = 'invalid_capex_sign' if fact.sign_policy in {'positive_outflow','negative_outflow'} else 'missing_capex_sign_policy'
                break
    if reason:
        return result(metric_id, facts, reason=reason, unit='currency_amount', materialized_at=materialized_at)
    return result(metric_id, facts, value=exact_difference(cfo.value,exact_sum(uses)), unit='currency_amount',
                  materialized_at=materialized_at, evidence={'capex_policy':'cash_ppe_and_intangible_excluding_acquisitions'})


@deterministic_decimal
def standard_ebitda(facts: Iterable[FinancialFact], *, as_of: datetime,
                    history_mode: str = 'reconstructed_public', materialized_at: datetime | None = None) -> MetricResult:
    facts = tuple(fact for fact in facts if fact.metric in {'net_income_total','tax_expense','net_interest_expense',
                                                          'interest_expense','interest_income','depreciation_amortization',
                                                          'depreciation','amortization'})
    by_metric = {metric:tuple(fact for fact in facts if fact.metric == metric) for metric in {fact.metric for fact in facts}}
    reason = materialization_reason(history_mode, as_of, materialized_at)
    if any(len(group) > 1 for group in by_metric.values()):
        reason = reason or 'overlapping_components'
    da_total = by_metric.get('depreciation_amortization', ())
    da_details = by_metric.get('depreciation', ()) + by_metric.get('amortization', ())
    interest_total = by_metric.get('net_interest_expense', ())
    interest_details = by_metric.get('interest_expense', ()) + by_metric.get('interest_income', ())
    if da_total and da_details or interest_total and interest_details or _overlap(facts):
        reason = reason or 'overlapping_components'
    if not all(by_metric.get(metric) for metric in ('net_income_total','tax_expense')) or not (da_total or len(da_details)==2) or not (interest_total or len(interest_details)==2):
        reason = reason or 'missing_ebitda_component'
    reason = reason or compatible_facts(facts, as_of=as_of, history_mode=history_mode, same_period=True)
    if reason is None and any(fact.scope != 'consolidated' for fact in facts):
        reason = 'consolidated_total_scope_required'
    if reason:
        return result('ebitda_standard', facts, reason=reason, unit='currency_amount', materialized_at=materialized_at)
    net_interest = interest_total[0].value if interest_total else exact_difference(by_metric['interest_expense'][0].value,by_metric['interest_income'][0].value)
    da = da_total[0].value if da_total else exact_sum(fact.value for fact in da_details)
    value = exact_sum((by_metric['net_income_total'][0].value,by_metric['tax_expense'][0].value,net_interest,da))
    return result('ebitda_standard', facts, value=value, unit='currency_amount', materialized_at=materialized_at,
                  evidence={'definition':'total_net_income_plus_tax_net_interest_and_da'})


def _growth_result(metric_id, inputs, current_value, previous_value, *, materialized_at=None, evidence=None):
    delta = exact_difference(current_value,previous_value)
    if previous_value <= 0:
        if previous_value == 0:
            flag = 'from_zero'
        elif current_value > 0:
            flag = 'loss_to_profit'
        elif current_value == 0:
            flag = 'loss_to_break_even'
        else:
            flag = 'loss_narrowed' if delta>0 else ('loss_widened' if delta<0 else 'loss_unchanged')
        return result(metric_id, inputs, reason='nonpositive_comparison', status='nonpositive_comparison',
                      flags=(flag,), absolute_delta=delta, materialized_at=materialized_at, evidence=evidence or {})
    flags = ('profit_to_loss',) if current_value<0 else ()
    return result(metric_id, inputs, value=exact_difference(current_value/previous_value,Decimal(1)), flags=flags,
                  absolute_delta=delta, materialized_at=materialized_at, evidence=evidence or {})


@deterministic_decimal
def growth(metric_id: str, facts: Iterable[FinancialFact], *, as_of: datetime,
            history_mode: str = 'reconstructed_public', materialized_at: datetime | None = None) -> MetricResult:
    registered = REQUIRED_GROWTH_METRICS+OPTIONAL_GROWTH_METRICS+ANNUAL_GROWTH_METRICS
    if metric_id not in registered:
        raise ValueError(f'unsupported_metric:{metric_id}')
    comparison = next(kind for kind in ('ttm_yoy','fy_yoy','qoq','yoy') if metric_id.endswith('_'+kind))
    source_metric = metric_id[len('growth_'):-(len(comparison)+1)]
    facts = tuple(fact for fact in select_facts_asof(facts, as_of=as_of, history_mode=history_mode) if fact.metric == source_metric)
    reason = materialization_reason(history_mode, as_of, materialized_at)
    inputs = ()
    current_value = previous_value = None
    evidence = {'comparison':comparison}
    if comparison == 'fy_yoy':
        annual = [fact for fact in facts if fact.period_kind == 'annual' and fact.directly_reported and fact.fiscal_year is not None]
        if not annual:
            reason = reason or 'missing_direct_annual_report'
        else:
            year = max(fact.fiscal_year for fact in annual)
            current = [fact for fact in annual if fact.fiscal_year == year]
            previous = [fact for fact in annual if fact.fiscal_year == year-1]
            inputs = tuple(current+previous)
            if len(current)!=1 or len(previous)!=1:
                reason = reason or ('ambiguous_input' if len(current)>1 or len(previous)>1 else 'missing_comparison_period')
            else:
                reason = reason or compatible_facts(inputs, as_of=as_of, history_mode=history_mode)
                if any(fact.period_start is None or not 350 <= (fact.period_end-fact.period_start).days+1 <= 380 for fact in inputs):
                    reason = reason or 'incompatible_period'
                if source_metric in EPS_METRICS and any(not fact.directly_reported for fact in inputs):
                    reason = reason or 'direct_annual_eps_required'
                current_value, previous_value = current[0].value,previous[0].value
                evidence.update(basis='direct_annual_report',period_start=current[0].period_start,period_end=current[0].period_end)
    else:
        current, missing = _latest_quarter(facts, source_metric)
        reason = reason or missing
        if current:
            if comparison == 'ttm_yoy':
                current_window, current_missing = _window(facts, source_metric, current.quarter_index)
                previous_window, previous_missing = _window(facts, source_metric, current.quarter_index-4)
                inputs = current_window+previous_window
                reason = reason or current_missing or previous_missing
                if reason is None:
                    current_result = sum_quarters(current_window, as_of=as_of,history_mode=history_mode,materialized_at=materialized_at)
                    previous_result = sum_quarters(previous_window, as_of=as_of,history_mode=history_mode,materialized_at=materialized_at)
                    reason = current_result.reason or previous_result.reason or compatible_facts(inputs,as_of=as_of,history_mode=history_mode)
                    current_value, previous_value = current_result.value,previous_result.value
                    evidence.update(current_window=current_result.evidence,previous_window=previous_result.evidence)
            else:
                previous_index = current.quarter_index-(1 if comparison == 'qoq' else 4)
                previous = _period_facts(facts, source_metric, previous_index)
                inputs = (current,)+previous
                if len(previous)!=1:
                    reason = reason or ('ambiguous_input' if previous else 'missing_comparison_period')
                else:
                    prior = previous[0]
                    reason = reason or compatible_facts(inputs,as_of=as_of,history_mode=history_mode)
                    if comparison == 'yoy' and (current.fiscal_year is None or prior.fiscal_year != current.fiscal_year-1 or prior.fiscal_quarter != current.fiscal_quarter):
                        reason = reason or 'incompatible_comparison_period'
                    if comparison == 'qoq' and (current.period_start is None or prior.period_end+timedelta(days=1)!=current.period_start):
                        reason = reason or 'incompatible_comparison_period'
                    if source_metric in EPS_METRICS and any(not fact.directly_reported for fact in inputs):
                        reason = reason or 'eps_requires_reported_quarters'
                    current_value,previous_value=current.value,prior.value
                    evidence.update(current_period_end=current.period_end,previous_period_end=prior.period_end)
    if reason:
        return result(metric_id,inputs,reason=reason,materialized_at=materialized_at,evidence=evidence)
    return _growth_result(metric_id,inputs,current_value,previous_value,materialized_at=materialized_at,evidence=evidence)


def _ratio(metric_id, facts, numerator, denominator, *, materialized_at=None, evidence=None):
    if denominator <= 0:
        return result(metric_id,facts,reason='nonpositive_denominator',status='nonpositive_denominator',
                      materialized_at=materialized_at,evidence=evidence or {})
    return result(metric_id,facts,value=numerator/denominator,materialized_at=materialized_at,evidence=evidence or {})


def _component_quarters(facts, end_index, function, *, as_of, history_mode, materialized_at):
    quarter_results = []
    used = []
    for index in range(end_index-3,end_index+1):
        quarter = tuple(fact for fact in facts if fact.period_kind=='quarter' and fact.quarter_index==index)
        if function == free_cash_flow:
            cfo = tuple(fact for fact in quarter if fact.metric=='cfo')
            if len(cfo)!=1:
                return tuple(used),None,'missing_cfo' if not cfo else 'ambiguous_input'
            components = tuple(fact for fact in quarter if fact.metric in {'cash_capex','cash_capex_ppe','cash_capex_intangible'})
            computed = function(cfo[0],components,as_of=as_of,history_mode=history_mode,materialized_at=materialized_at)
            used.extend((cfo[0],)+components)
        else:
            components = tuple(fact for fact in quarter if fact.metric in {'net_income_total','tax_expense','net_interest_expense',
                                      'interest_expense','interest_income','depreciation_amortization','depreciation','amortization'})
            computed = function(components,as_of=as_of,history_mode=history_mode,materialized_at=materialized_at)
            used.extend(components)
        if computed.reason:
            return tuple(used),None,computed.reason
        quarter_results.append(computed.value)
    reason = compatible_facts(used,as_of=as_of,history_mode=history_mode)
    if reason is None:
        anchors = tuple(next(fact for fact in used if fact.quarter_index==index) for index in range(end_index-3,end_index+1))
        # Verify real dates across quarters without pretending different metrics are one series.
        if any(b.period_start != a.period_end+timedelta(days=1) for a,b in zip(anchors,anchors[1:])):
            reason='nonconsecutive_periods'
    return tuple(used),exact_sum(quarter_results) if reason is None else None,reason


@deterministic_decimal
def calculate_metric(metric_id: str, facts: Iterable[FinancialFact], *, as_of: datetime,
                     history_mode: str = 'reconstructed_public', materialized_at: datetime | None = None) -> MetricResult:
    facts = tuple(facts)
    if metric_id.startswith('growth_'):
        return growth(metric_id,facts,as_of=as_of,history_mode=history_mode,materialized_at=materialized_at)
    reason = materialization_reason(history_mode,as_of,materialized_at)
    facts = select_facts_asof(facts,as_of=as_of,history_mode=history_mode)
    options = dict(as_of=as_of,history_mode=history_mode,materialized_at=materialized_at)
    if metric_id in {'eps_basic_ttm','eps_diluted_ttm','revenue_ttm','operating_income_ttm','net_income_total_ttm','net_income_parent_ttm','cfo_ttm'}:
        source_metric=metric_id.removesuffix('_ttm')
        anchor,missing=_latest_quarter(facts,source_metric)
        if anchor is None:
            return result(metric_id,(),reason=reason or missing,materialized_at=materialized_at)
        window,missing=_window(facts,source_metric,anchor.quarter_index)
        return result(metric_id,window,reason=reason or missing,materialized_at=materialized_at) if reason or missing else sum_quarters(window,metric_id=metric_id,**options)
    if metric_id in {'fcf_cfo_less_capex_q','fcf_cfo_less_capex_ttm','ebitda_standard_ttm'}:
        anchor,missing=_latest_quarter(facts,'cfo' if metric_id.startswith('fcf') else 'net_income_total')
        if anchor is None:
            return result(metric_id,(),reason=reason or missing,unit='currency_amount',materialized_at=materialized_at)
        function=free_cash_flow if metric_id.startswith('fcf') else standard_ebitda
        if metric_id.endswith('_q'):
            quarter=tuple(fact for fact in facts if fact.period_kind=='quarter' and fact.quarter_index==anchor.quarter_index)
            return free_cash_flow(anchor,quarter,**options)
        used,value,missing=_component_quarters(facts,anchor.quarter_index,function,**options)
        return result(metric_id,used,value=value,reason=reason or missing,unit='currency_amount',materialized_at=materialized_at)
    average_metrics={'roe_parent_ttm':('net_income_parent','equity_parent','parent'),
                     'roa_total_ttm':('net_income_total','assets_total','consolidated')}
    margin_metrics={'gross_margin_ttm':'gross_profit','operating_margin_ttm':'operating_income',
                    'net_margin_ttm':'net_income_total','ebitda_margin_ttm':'ebitda_standard',
                    'fcf_margin_ttm':'fcf'}
    if metric_id in average_metrics:
        source_metric,balance_metric,scope=average_metrics[metric_id]
        anchor,missing=_latest_quarter(facts,source_metric)
        if anchor is None:
            return result(metric_id,(),reason=reason or missing,materialized_at=materialized_at)
        window,missing=_window(facts,source_metric,anchor.quarter_index)
        inputs=window
        reason=reason or missing
        if reason is None:
            summed=sum_quarters(window,**options)
            reason=summed.reason
            if reason:
                return result(metric_id,inputs,reason=reason,materialized_at=materialized_at)
            opening=tuple(fact for fact in facts if fact.metric==balance_metric and fact.period_kind=='instant' and fact.period_end==window[0].period_start-timedelta(days=1))
            closing=tuple(fact for fact in facts if fact.metric==balance_metric and fact.period_kind=='instant' and fact.period_end==anchor.period_end)
            inputs=window+opening+closing
            if len(opening)!=1:
                reason=reason or ('ambiguous_input' if opening else 'missing_opening_balance')
            if len(closing)!=1:
                reason=reason or ('ambiguous_input' if closing else 'missing_closing_balance')
            reason=reason or compatible_facts(inputs,as_of=as_of,history_mode=history_mode)
            if any(fact.scope!=scope for fact in inputs):
                reason=reason or 'scope_mismatch'
            if reason is None:
                return _ratio(metric_id,inputs,summed.value,exact_multiply(exact_sum((opening[0].value,closing[0].value)),Decimal('.5')),
                              materialized_at=materialized_at,evidence={'average_method':'opening_closing_arithmetic_mean',
                               'period_start':window[0].period_start,'period_end':anchor.period_end})
        return result(metric_id,inputs,reason=reason,materialized_at=materialized_at)
    if metric_id in margin_metrics:
        anchor,missing=_latest_quarter(facts,'revenue')
        if anchor is None:
            return result(metric_id,(),reason=reason or missing,materialized_at=materialized_at)
        revenue,missing=_window(facts,'revenue',anchor.quarter_index)
        reason=reason or missing
        denominator=sum_quarters(revenue,**options)
        reason=reason or denominator.reason
        source_metric=margin_metrics[metric_id]
        if source_metric in {'fcf','ebitda_standard'}:
            numerator,value,missing=_component_quarters(facts,anchor.quarter_index,free_cash_flow if source_metric=='fcf' else standard_ebitda,**options)
            reason=reason or missing
        else:
            numerator,missing=_window(facts,source_metric,anchor.quarter_index)
            reason=reason or missing
            summed=sum_quarters(numerator,**options)
            value=summed.value
            reason=reason or summed.reason
        inputs=revenue+numerator
        reason=reason or compatible_facts(inputs,as_of=as_of,history_mode=history_mode)
        revenue_periods={fact.quarter_index:(fact.period_kind,fact.period_start,fact.period_end) for fact in revenue}
        if any(revenue_periods.get(fact.quarter_index)!=(fact.period_kind,fact.period_start,fact.period_end)
               for fact in numerator):
            reason=reason or 'period_mismatch'
        if any(fact.scope!='consolidated' for fact in inputs):
            reason=reason or 'scope_mismatch'
        if reason:
            return result(metric_id,inputs,reason=reason,materialized_at=materialized_at)
        return _ratio(metric_id,inputs,value,denominator.value,materialized_at=materialized_at)
    raise ValueError(f'unsupported_metric:{metric_id}')
