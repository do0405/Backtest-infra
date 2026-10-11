"""v7 raw-price market clock; acquisition timestamps stay physically unchanged."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..prices.models import PriceObservation

POLICY='completed_local_day_raw_price_v1'
DAILY_POLICY='completed_local_day_supplier_basis_v1'
MARKET_ZONES={'KR':'Asia/Seoul','US':'America/New_York'}


def enabled(rows):
    return getattr(rows,'manifest',{}).get('policies',{}).get('query_version') in {'release_query_v7','release_query_v8_daily'}


def availability(row,rows):
    if not isinstance(row,PriceObservation):return None
    market=rows.manifest.get('catalog',{}).get('securities',{}).get(row.security_id,{}).get('market')
    if rows.manifest.get('policies',{}).get('query_version')=='release_query_v8_daily':
        from ..prices.provider_basis import contract_price_basis
        invalid={'status':'unavailable','effective_available_at':None,'reason':'supplier_price_basis_unqualified','policy':DAILY_POLICY}
        contract=rows.get('source_contract',{}).get(row.source_contract_id)
        if contract is None or market not in MARKET_ZONES or row.session is None:return invalid
        try:definition=contract_price_basis(contract)
        except (ValueError,TypeError,AttributeError):return invalid
        if row.basis!=definition['basis']:return invalid
        recorded=(row.evidence or {}).get('price_basis_policy')
        # Existing raw rows can rely on their exact frozen source contract.
        # A recorded declaration, when present, must match its action semantics.
        if recorded is not None and (not isinstance(recorded,dict) or any(
                recorded.get(key)!=definition[key] for key in ('policy','basis','adjustments','volume_basis'))):return invalid
        bound=datetime.combine(row.session+timedelta(days=1),time.min,ZoneInfo(MARKET_ZONES[market])).astimezone(timezone.utc)
        return {'status':'market_reconstructed','effective_available_at':bound,'policy':DAILY_POLICY,
            'price_basis_policy':definition,'acquisition_pit_required':False,'historical_revision_time_verified':False,
            'adjustment_action_coverage_verified':False}
    if row.basis!='as_traded' or market not in MARKET_ZONES or row.session is None:
        return {'status':'unavailable','effective_available_at':None,'reason':'unsupported_raw_market_clock','policy':POLICY}
    # Conservative daily policy: includes early-close and DST days without
    # inventing historical exchange calendar evidence or intraday availability.
    bound=datetime.combine(row.session+timedelta(days=1),time.min,ZoneInfo(MARKET_ZONES[market])).astimezone(timezone.utc)
    return {'status':'market_reconstructed','effective_available_at':bound,'policy':POLICY,
        'acquisition_pit_required':False,'historical_revision_time_verified':False,
        'adjustment_action_coverage_verified':False}
