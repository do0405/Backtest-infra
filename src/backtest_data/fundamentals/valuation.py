"""PIT raw-price valuation, complete share classes and book-basis EV."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from .domain import FinancialFact, MetricResult, aware
from .metric_policy import compatible_facts, input_ids, materialization_reason
from .ratios import calculate_metric
from .selection import fact_visible, select_facts_asof
from .numeric import NUMERIC_POLICY, deterministic_decimal, exact_difference, exact_multiply, exact_sum


@dataclass(frozen=True, slots=True)
class ValuationPrice:
    id: str
    security_id: str
    listing_id: str
    value: Decimal
    currency: str
    share_basis: str
    available_at: datetime | None
    captured_at: datetime
    decision_recorded_at: datetime | None = None
    basis: str = 'as_traded'
    complete: bool = True

    def __post_init__(self):
        for name in ('available_at','captured_at','decision_recorded_at'):
            aware(getattr(self,name),name)
        if not isinstance(self.value,Decimal) or not self.value.is_finite() or self.value<0:
            raise ValueError('nonnegative_finite_price_required')


@dataclass(frozen=True, slots=True)
class CommonShareClass:
    class_id: str
    price: ValuationPrice
    shares: FinancialFact
    is_primary_listing: bool = True


@dataclass(frozen=True, slots=True)
class FXRate:
    id: str
    from_currency: str
    to_currency: str
    rate: Decimal
    available_at: datetime
    captured_at: datetime
    verified: bool = False
    definition_version: str | None = None
    decision_recorded_at: datetime | None = None

    def __post_init__(self):
        for name in ('available_at','captured_at','decision_recorded_at'):
            aware(getattr(self,name),name)
        if not isinstance(self.rate,Decimal) or not self.rate.is_finite() or self.rate<=0:
            raise ValueError('positive_finite_fx_rate_required')


def _price_reason(price,as_of,history_mode):
    if price.basis!='as_traded':
        return 'raw_price_required'
    if not price.complete:
        return 'incomplete_price'
    if price.available_at is None or price.available_at>as_of:
        return 'future_or_unobserved_price'
    if history_mode=='observed_system' and (price.captured_at>as_of or price.decision_recorded_at is None or price.decision_recorded_at>as_of):
        return 'future_or_unobserved_price'
    if not price.currency or not price.share_basis:
        return 'missing_price_basis'
    return None


def _convert(value,currency,target,fx_rates,as_of,history_mode):
    if not currency or not target:
        return None,None,'currency_mismatch'
    if currency==target:
        return value,None,None
    valid=[fx for fx in fx_rates if fx.from_currency==currency and fx.to_currency==target and fx.verified
           and fx.definition_version and fx.available_at<=as_of and
           (history_mode!='observed_system' or fx.captured_at<=as_of and fx.decision_recorded_at is not None and fx.decision_recorded_at<=as_of)]
    if len(valid)!=1:
        return None,None,'currency_mismatch'
    return exact_multiply(value,valid[0].rate),valid[0],None


def _value_result(metric_id,value,reason,ids,available,*,unit='currency_amount',currency=None,
                   materialized_at=None,share_basis=None,scope='consolidated',flags=(),evidence=None,status=None):
    return MetricResult(metric_id,value if reason is None else None,status or ('ok' if reason is None else 'unavailable'),
                        reason,unit,tuple(dict.fromkeys(ids)),max(available) if available else None,materialized_at,
                        definition_version='financial_metrics_annual_v1' if metric_id=='per_diluted_fy' else 'financial_metrics_v1',
                        currency=currency,scope=scope,share_basis=share_basis,flags=flags,
                        evidence={**(evidence or {}),'numeric_policy':NUMERIC_POLICY})


def _metric_reason(metric,as_of,history_mode):
    if metric is None or metric.status!='ok' or metric.value is None:
        return metric.reason if metric is not None and metric.reason else 'missing_input'
    if metric.available_at is None or metric.available_at>as_of:
        return 'future_or_unobserved_input'
    if history_mode=='observed_system' and (metric.materialized_at is None or metric.materialized_at>as_of):
        return 'materialization_after_cutoff'
    return None


@deterministic_decimal
def market_cap_common(classes: Iterable[CommonShareClass], *, required_class_ids: set[str], as_of: datetime,
                       history_mode: str = 'reconstructed_public', materialized_at: datetime | None = None,
                       target_currency: str | None = None, fx_rates: Iterable[FXRate] = ()) -> MetricResult:
    classes=tuple(classes)
    fx_rates=tuple(fx_rates)
    currency=target_currency or (classes[0].price.currency if classes else None)
    reason=materialization_reason(history_mode,as_of,materialized_at)
    if not required_class_ids or {share_class.class_id for share_class in classes}!=set(required_class_ids):
        reason=reason or 'capitalization_coverage_incomplete'
    selected=[]
    securities=set()
    for class_id in sorted(required_class_ids):
        matches=[share_class for share_class in classes if share_class.class_id==class_id and share_class.is_primary_listing]
        if len(matches)!=1:
            reason=reason or 'capitalization_coverage_incomplete'
            continue
        share_class=matches[0]
        if share_class.price.security_id in securities:
            reason=reason or 'duplicate_security_class'
        securities.add(share_class.price.security_id)
        selected.append(share_class)
    if len({item.shares.release_id for item in selected if item.shares.release_id is not None})>1:
        reason=reason or 'release_mismatch'
    ids=[]
    available=[]
    values=[]
    for share_class in selected:
        price,shares=share_class.price,share_class.shares
        ids.extend((price.id,)+shares.input_ids)
        available.extend(value for value in (price.available_at,shares.available_at) if value is not None)
        reason=reason or _price_reason(price,as_of,history_mode)
        if shares.metric!='shares_outstanding_common' or shares.period_kind!='instant' or shares.unit!='shares':
            reason=reason or 'pit_outstanding_shares_required'
        if not fact_visible(shares,as_of=as_of,history_mode=history_mode):
            reason=reason or 'future_or_unobserved_input'
        if shares.period_end>as_of.date():
            reason=reason or 'future_outstanding_snapshot'
        if shares.share_basis!=price.share_basis:
            reason=reason or 'share_basis_mismatch'
        if shares.value is None or shares.value<0:
            reason=reason or 'invalid_outstanding_shares'
        if reason is None:
            value,fx,fx_reason=_convert(exact_multiply(price.value,shares.value),price.currency,currency,fx_rates,as_of,history_mode)
            reason=reason or fx_reason
            if fx:
                ids.append(fx.id)
                available.append(fx.available_at)
            if value is not None:
                values.append(value)
    total=exact_sum(values)
    return _value_result('market_cap_common_pit',total,reason,ids,available,currency=currency,
                         materialized_at=materialized_at,scope='issuer_common',
                         evidence={'coverage_complete':reason is None,'common_class_ids':tuple(sorted(required_class_ids)),
                                   'snapshot_dates':{item.class_id:item.shares.period_end for item in selected},
                                   'reported_share_based_estimate':True,'listing_policy':'one_explicit_primary_listing_per_security'})


@deterministic_decimal
def per_diluted(price: ValuationPrice, facts: Iterable[FinancialFact], *, as_of: datetime,
                 history_mode: str = 'reconstructed_public', materialized_at: datetime | None = None,
                 annual: bool = False, fx_rates: Iterable[FXRate] = ()) -> MetricResult:
    metric_id='per_diluted_fy' if annual else 'per_diluted_ttm'
    reason=materialization_reason(history_mode,as_of,materialized_at) or _price_reason(price,as_of,history_mode)
    facts=select_facts_asof(facts,as_of=as_of,history_mode=history_mode)
    ids=[price.id]
    available=[price.available_at] if price.available_at else []
    eps_value=None
    evidence={}
    eps_currency=None
    if annual:
        annual_facts=[fact for fact in facts if fact.metric=='eps_diluted' and fact.period_kind=='annual' and fact.directly_reported]
        if not annual_facts:
            reason=reason or 'missing_direct_annual_report'
        else:
            end=max(fact.period_end for fact in annual_facts)
            selected=tuple(fact for fact in annual_facts if fact.period_end==end)
            if len(selected)!=1:
                reason=reason or 'ambiguous_input'
            reason=reason or compatible_facts(selected,as_of=as_of,history_mode=history_mode)
            ids.extend(input_ids(selected))
            available.extend(fact.available_at for fact in selected if fact.available_at)
            eps_value=selected[0].value
            eps_currency=selected[0].currency
            basis=selected[0].share_basis
            if selected[0].period_start is None or not 350<=(selected[0].period_end-selected[0].period_start).days+1<=380:
                reason=reason or 'incompatible_period'
            evidence={'eps_method':'direct_annual_report','fiscal_end':end,'snapshot_age_days':(as_of.date()-end).days}
    else:
        eps=calculate_metric('eps_diluted_ttm',facts,as_of=as_of,history_mode=history_mode,materialized_at=materialized_at)
        reason=reason or eps.reason
        ids.extend(eps.input_record_ids)
        if eps.available_at:
            available.append(eps.available_at)
        eps_value,eps_currency,basis=eps.value,eps.currency,eps.share_basis
        evidence=dict(eps.evidence)
    if eps_value is not None:
        if basis!=price.share_basis:
            reason=reason or 'share_basis_mismatch'
        if eps_value<=0:
            reason=reason or 'nonpositive_eps'
    value=None
    if reason is None:
        converted,fx,reason=_convert(price.value,price.currency,eps_currency,tuple(fx_rates),as_of,history_mode)
        if fx:
            ids.append(fx.id)
            available.append(fx.available_at)
        if reason is None:
            value=converted/eps_value
    return _value_result(metric_id,value,reason,ids,available,unit='multiple',currency=eps_currency,
                         materialized_at=materialized_at,share_basis=price.share_basis,scope='parent_common',evidence=evidence)


@deterministic_decimal
def pbr_common(capitalization: MetricResult, equity_parent: FinancialFact | None,
                 preferred_equity: FinancialFact | None, *, as_of: datetime,
                 history_mode: str = 'reconstructed_public', materialized_at: datetime | None = None,
                 fx_rates: Iterable[FXRate] = ()) -> MetricResult:
    reason=materialization_reason(history_mode,as_of,materialized_at) or _metric_reason(capitalization,as_of,history_mode)
    if capitalization.metric_id!='market_cap_common_pit' or not capitalization.evidence.get('coverage_complete'):
        reason=reason or 'capitalization_coverage_incomplete'
    if equity_parent is None:
        reason=reason or 'missing_parent_equity'
    if preferred_equity is None:
        reason=reason or 'missing_preferred_equity'
    facts=tuple(fact for fact in (equity_parent,preferred_equity) if fact is not None)
    reason=reason or compatible_facts(facts,as_of=as_of,history_mode=history_mode,same_period=True)
    if equity_parent and (equity_parent.metric!='equity_parent' or equity_parent.scope!='parent' or equity_parent.period_kind!='instant'):
        reason=reason or 'parent_equity_required'
    if preferred_equity and preferred_equity.metric!='preferred_equity_capital':
        reason=reason or 'equity_classified_preferred_required'
    ids=list(capitalization.input_record_ids)+list(input_ids(facts))
    available=[fact.available_at for fact in facts if fact.available_at]
    if capitalization.available_at:
        available.append(capitalization.available_at)
    value=None
    common_equity=None
    if reason is None:
        common_equity=exact_difference(equity_parent.value,preferred_equity.value)
        if common_equity<=0:
            reason='nonpositive_common_equity'
        else:
            cap,fx,reason=_convert(capitalization.value,capitalization.currency,equity_parent.currency,tuple(fx_rates),as_of,history_mode)
            if fx:
                ids.append(fx.id)
                available.append(fx.available_at)
            if reason is None:
                value=cap/common_equity
    return _value_result('pbr_common',value,reason,ids,available,unit='multiple',currency=equity_parent.currency if equity_parent else None,
                         materialized_at=materialized_at,scope='parent_common',evidence={'common_equity':common_equity})


@deterministic_decimal
def enterprise_value(capitalization: MetricResult, debt: Iterable[FinancialFact],
                       preferred: FinancialFact | None, nci: FinancialFact | None, cash: FinancialFact | None, *,
                       as_of: datetime, history_mode: str = 'reconstructed_public', materialized_at: datetime | None = None,
                       fx_rates: Iterable[FXRate] = ()) -> MetricResult:
    debt=tuple(debt)
    fx_rates=tuple(fx_rates)
    facts=debt+tuple(fact for fact in (preferred,nci,cash) if fact is not None)
    reason=materialization_reason(history_mode,as_of,materialized_at) or _metric_reason(capitalization,as_of,history_mode)
    if capitalization.metric_id!='market_cap_common_pit' or not capitalization.evidence.get('coverage_complete'):
        reason=reason or 'capitalization_coverage_incomplete'
    if not debt:
        reason=reason or 'missing_interest_bearing_debt'
    if preferred is None or nci is None or cash is None:
        reason=reason or 'missing_ev_component'
    totals=[fact for fact in debt if fact.metric=='interest_bearing_debt']
    if totals and len(debt)>1:
        reason=reason or 'overlapping_debt_components'
    lease_details=tuple(fact for fact in debt if fact.metric in {'lease_debt','lease_debt_current','lease_debt_noncurrent'})
    if lease_details and any(fact.metric in {'interest_debt_current','interest_debt_noncurrent'}
                             and fact.evidence.get('includes_leases') is True for fact in debt):
        reason=reason or 'overlapping_debt_components'
    components=set()
    for fact in debt:
        coverage=set(fact.component_ids) or {fact.metric}
        if components & coverage:
            reason=reason or 'overlapping_debt_components'
        components.update(coverage)
    if len(totals)==1:
        if not totals[0].evidence.get('includes_leases'):
            reason=reason or 'lease_coverage_unverified'
    elif {fact.metric for fact in debt}=={'interest_debt_current','interest_debt_noncurrent'}:
        if not all(fact.evidence.get('includes_leases') for fact in debt):
            reason=reason or 'lease_coverage_unverified'
    elif {fact.metric for fact in debt}!={'interest_debt_current','interest_debt_noncurrent','lease_debt_current','lease_debt_noncurrent'}:
        reason=reason or 'incomplete_debt_components'
    reason=reason or compatible_facts(facts,as_of=as_of,history_mode=history_mode,check_scope=False,
                                      check_currency=False,check_unit=False,same_period=True)
    if any(fact.period_kind!='instant' for fact in facts):
        reason=reason or 'instant_ev_components_required'
    if preferred and preferred.metric!='preferred_equity_capital' or nci and nci.metric!='nci_equity' or cash and cash.metric!='cash_equivalents':
        reason=reason or 'invalid_ev_component'
    if any(fact.scope!='consolidated' for fact in debt+tuple(fact for fact in (nci,cash) if fact is not None)):
        reason=reason or 'consolidated_scope_required'
    if any(fact.value is not None and fact.value<0 for fact in debt+tuple(fact for fact in (preferred,cash) if fact is not None)):
        reason=reason or 'negative_ev_component'
    ids=list(capitalization.input_record_ids)+list(input_ids(facts))
    available=[fact.available_at for fact in facts if fact.available_at]
    if capitalization.available_at:
        available.append(capitalization.available_at)
    value=None
    if reason is None:
        converted=[]
        for fact in facts:
            converted_value,fx,fx_reason=_convert(fact.value,fact.currency,capitalization.currency,fx_rates,as_of,history_mode)
            reason=reason or fx_reason
            if fx:
                ids.append(fx.id)
                available.append(fx.available_at)
            converted.append(converted_value)
        if reason is None:
            value=exact_sum((capitalization.value,*converted[:-1],converted[-1].copy_negate()))
    flags=('negative_ev',) if value is not None and value<0 else ()
    return _value_result('enterprise_value_book_basis',value,reason,ids,available,currency=capitalization.currency,
                         materialized_at=materialized_at,flags=flags,evidence={'preferred_nci_basis':'book_basis','debt_includes_leases':reason is None})


@deterministic_decimal
def ev_ebitda(ev: MetricResult, ebitda: MetricResult, *, as_of: datetime,
               history_mode: str = 'reconstructed_public', materialized_at: datetime | None = None,
               fx_rates: Iterable[FXRate] = ()) -> MetricResult:
    reason=materialization_reason(history_mode,as_of,materialized_at) or _metric_reason(ev,as_of,history_mode) or _metric_reason(ebitda,as_of,history_mode)
    if ev.metric_id!='enterprise_value_book_basis':
        reason=reason or 'book_basis_ev_required'
    if ebitda.metric_id!='ebitda_standard_ttm' or ebitda.definition_version!='financial_metrics_v1' or ebitda.scope!='consolidated':
        reason=reason or 'standard_ebitda_ttm_required'
    if ebitda.value is not None and ebitda.value<=0:
        reason=reason or 'nonpositive_ebitda'
    ids=list(ev.input_record_ids+ebitda.input_record_ids)
    available=[item.available_at for item in (ev,ebitda) if item.available_at]
    value=None
    if reason is None:
        converted,fx,reason=_convert(ev.value,ev.currency,ebitda.currency,tuple(fx_rates),as_of,history_mode)
        if fx:
            ids.append(fx.id)
            available.append(fx.available_at)
        if reason is None:
            value=converted/ebitda.value
    flags=('negative_ev',) if value is not None and value<0 else ()
    return _value_result('ev_ebitda_ttm',value,reason,ids,available,unit='multiple',currency=ebitda.currency,
                         materialized_at=materialized_at,flags=flags,evidence={'preferred_nci_basis':'book_basis'})
