"""Explicit release-bound quality and disclosure policy selection."""

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
import hashlib
import json

from ..fundamentals.numeric import NUMERIC_POLICY
from ..fundamentals.metric_policy import ANNUAL_GROWTH_METRICS,OPTIONAL_GROWTH_METRICS,REQUIRED_GROWTH_METRICS
from ..prices.numeric import NUMERIC_POLICY as PRICE_NUMERIC_POLICY

PROFILES={'fundamentals_sql','core_price_volume','exact_vwap','valuation','index_membership','security_identity'}


@lru_cache(maxsize=1)
def _definition_registry():
    formulas={
        'roe_parent_ttm':'parent_net_income_4q / arithmetic_mean(parent_equity_open,parent_equity_close)',
        'roa_total_ttm':'total_net_income_4q / arithmetic_mean(total_assets_open,total_assets_close)',
        'gross_margin_ttm':'gross_profit_4q / revenue_4q',
        'operating_margin_ttm':'operating_income_4q / revenue_4q',
        'net_margin_ttm':'total_net_income_4q / revenue_4q',
        'ebitda_margin_ttm':'standard_ebitda_4q / revenue_4q',
        'fcf_margin_ttm':'(cfo_4q-cash_ppe_and_intangible_capex_4q) / revenue_4q',
        'fcf_cfo_less_capex_q':'cfo_q-cash_ppe_and_intangible_capex_q; explicit_outflow_sign; acquisitions_excluded',
        'fcf_cfo_less_capex_ttm':'sum_4q(cfo_q-cash_ppe_and_intangible_capex_q); no_missing_zero',
        'ebitda_standard_ttm':'sum_4q(total_net_income+tax+net_interest+depreciation_amortization); nonoverlapping_components',
        'market_cap_common_pit':'sum_common_classes(raw_price*pit_outstanding_shares); one_listing_per_security; complete_classes',
        'per_diluted_ttm':'raw_price / sum_4q(direct_reported_diluted_eps_rebased_to_price_basis); positive_eps',
        'per_diluted_fy':'raw_price / direct_reported_annual_diluted_eps_rebased_to_price_basis; positive_eps',
        'pbr_common':'common_market_cap / (parent_equity-equity_classified_preferred); positive_common_equity',
        'enterprise_value_book_basis':'common_market_cap+interest_debt_including_leases+preferred_book+nci_book-cash_equivalents',
        'ev_ebitda_ttm':'book_basis_enterprise_value / standard_ebitda_4q; positive_ebitda',
    }
    for source in ('eps_basic','eps_diluted','revenue','operating_income','net_income_total','net_income_parent','cfo'):
        formulas[source+'_ttm']='sum_4_compatible_consecutive_quarters('+source+'); EPS_direct_reported_only; compatible_share_basis'
    for metric in REQUIRED_GROWTH_METRICS+OPTIONAL_GROWTH_METRICS+ANNUAL_GROWTH_METRICS:
        comparison=next(suffix for suffix in ('ttm_yoy','fy_yoy','qoq','yoy') if metric.endswith('_'+suffix))
        formulas[metric]='current/previous-1; fiscal_comparison='+comparison+'; previous_positive; loss_transition_flags; same_scope_currency_share_basis'
    registry={}
    for metric,formula in sorted(formulas.items()):
        definition={'metric_id':metric,'definition_version':'financial_metrics_annual_v1' if metric.endswith('_fy_yoy') or metric=='per_diluted_fy' else 'financial_metrics_v1',
            'numeric_policy':NUMERIC_POLICY,'formula':formula,'eligibility_contract':'financial_metric_catalog_v1_no_implicit_zero_no_future_inputs'}
        definition['definition_hash']=hashlib.sha256(json.dumps(definition,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        registry[metric]=definition
    return registry


def definition_registry():return {metric:dict(value) for metric,value in _definition_registry().items()}


def registered_definition(row):
    definition=_definition_registry().get(row.metric_id)
    return bool(definition and row.definition_version==definition['definition_version'] and row.definition_hash==definition['definition_hash']
                and (row.basis or {}).get('numeric_policy')==definition['numeric_policy'])


def valid_metric_state(row):
    """Stored values and explicit missing-result states have distinct contracts."""
    if row.value is not None:return row.status=='ok'
    return row.status in {'unavailable','not_applicable','nonpositive_denominator','nonpositive_comparison'}


def definition_registry_hash():
    return hashlib.sha256(json.dumps(_definition_registry(),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def policy_bundle(query_version='release_query_v5'):
    from ..catalog.aliases import alias_policy
    if query_version not in {'release_query_v5','release_query_v6','release_query_v7','release_query_v8_daily'}:
        raise ValueError('unsupported_policy_version:query_version')
    bundle={'query_version':'release_query_v5','quality_contract_version':'release_quality_v1',
            'security_alias_policy':alias_policy(),
            'source_account_selection':'source_accounts_v3','canonical_selection':'frozen_adopted_lineage_all_visible_v1',
            'price_selection':'raw_price_structure_v1',
            'history_clock_policy':'public_available_observed_recorded_v2',
            'economic_date_policy':'stored_business_date_utc_cutoff_v1',
            'public_availability_policy':'conservative_stored_clocks_v1',
            'disclosure_policy_version':'disclosure_periodic_v1','disclosure_profile':'periodic_report',
            'calendar_policy':'frozen_calendar_snapshot_v1','financial_numeric_policy':NUMERIC_POLICY,
            'price_numeric_policy':PRICE_NUMERIC_POLICY,
            'metric_definitions':['financial_metrics_v1','financial_metrics_annual_v1'],
            'metric_registry_version':'financial_definition_registry_v1','metric_registry_hash':definition_registry_hash()}
    if query_version in {'release_query_v6','release_query_v7'}:
        bundle.update(query_version=query_version,history_clock_policy='verified_public_physical_observed_v1',
            public_availability_policy='financial_public_pit_v1',source_account_selection='source_accounts_public_v1',
            canonical_selection='frozen_public_lineage_v1')
    if query_version=='release_query_v7':
        bundle.update(price_selection='raw_price_completed_local_day_v1',
            price_availability_policy='completed_local_day_raw_price_v1',
            financial_certificate_required=True,
            adjustment_policy='verified_actions_through_query_cutoff_only_v1')
    if query_version=='release_query_v8_daily':
        bundle.update(query_version=query_version,history_clock_policy='source_metadata_next_session_v1',
            public_availability_policy='financial_daily_metadata_pit_v1',source_account_selection='source_accounts_daily_metadata_v1',
            canonical_selection='frozen_daily_metadata_lineage_v1',price_selection='supplier_declared_completed_day_v1',
            price_availability_policy='completed_local_day_supplier_basis_v1',financial_certificate_required=False,
            financial_daily_metadata_required=True,adjustment_policy='supplier_declared_price_basis_v1',
            corporate_action_role='supplemental',public_metadata_grade='metadata_grade')
    return bundle


@dataclass(frozen=True,slots=True)
class QueryContext:
    release_id: str
    as_of: datetime
    history_mode: str
    required_profile: str
    disclosure_profile: str
    research_partial: bool = False

    def __post_init__(self):
        if not self.release_id or self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError('explicit_release_and_aware_cutoff_required')
        if self.history_mode not in {'reconstructed_public','observed_system'}:
            raise ValueError('unsupported_history_mode')
        if self.required_profile not in PROFILES:
            raise ValueError('unsupported_quality_profile')
        expected='periodic_report' if self.required_profile in {'fundamentals_sql','valuation'} else 'not_applicable'
        if self.disclosure_profile!=expected:
            raise ValueError('unsupported_or_mixed_disclosure_profile')


def bind_context(context: QueryContext,manifest: dict) -> dict:
    pinned=manifest.get('policies',{})
    for key,value in policy_bundle(pinned.get('query_version')).items():
        if pinned.get(key)!=value:
            raise ValueError(f'unsupported_policy_version:{key}')
    if 'metric_definition_registry' in manifest and manifest['metric_definition_registry']!=definition_registry():
        raise ValueError('unsupported_metric_definition_registry')
    if manifest.get('tier')!='production' and not context.research_partial:
        raise ValueError('research_partial_option_required')
    if not manifest.get('coverage',{}).get('profiles',{}).get(context.required_profile,False) and not context.research_partial:
        raise ValueError('required_quality_profile_incomplete')
    return pinned
