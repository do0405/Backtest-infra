"""Supplier-declared price basis, independent of corporate-action completeness."""
from datetime import datetime,date,time,timedelta,timezone
from zoneinfo import ZoneInfo
import re

POLICY='supplier_declared_price_basis_v1'
DOMESTIC='/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice'
OVERSEAS='/uapi/overseas-price/v1/quotations/dailyprice'

def classify_price_basis(source,endpoint,params):
    adjustments=[];volume='unknown';reference=None
    if source=='kis' and endpoint==DOMESTIC:
        flag=params.get('FID_ORG_ADJ_PRC')
        if flag not in {'0','1'}:raise ValueError('Unknown domestic raw/adjusted flag')
        adjusted=flag=='0';adjustments=['provider_unspecified_actions'] if adjusted else []
        volume='provider_reported_scope_unknown' if adjusted else 'as_traded'
    elif source=='kis_overseas' and endpoint==OVERSEAS:
        flag=params.get('MODP')
        if flag not in {'0','1'}:raise ValueError('Unknown overseas raw/adjusted flag')
        adjusted=flag=='1';adjustments=['provider_unspecified_actions'] if adjusted else []
        volume='provider_reported_scope_unknown' if adjusted else 'as_traded'
    elif source=='alpaca' and re.fullmatch(r'/v2/stocks/[^/]+/bars',endpoint):
        flag=params.get('adjustment')
        allowed={'raw','split','dividend','spin-off','all'}
        terms=str(flag).split(',')
        if not terms or any(t not in allowed for t in terms) or len(set(terms))!=len(terms):raise ValueError('Unknown Alpaca adjustment')
        if any(t in terms for t in ('raw','all')) and len(terms)!=1:raise ValueError('Conflicting adjustment combination')
        adjusted=flag!='raw'
        adjustments=['split','dividend','spin-off'] if flag=='all' else terms if adjusted else []
        volume='split_adjusted' if 'split' in adjustments else 'as_traded'
    else:raise ValueError('Unclassified provider endpoint')
    return {'policy':POLICY,'basis':'provider_adjusted' if adjusted else 'as_traded',
        'source':source,'endpoint':endpoint,'request_flag':flag,'adjustments':adjustments,
        'volume_basis':volume,'evidence_grade':'supplier_declared',
        'adjustment_reference_date':reference,'historical_adjustment_reference_verified':False,
        'corporate_action_completeness_required':False,'corporate_action_role':'supplemental',
        'comparison_requires_same_adjustment_definition':True}

def supplier_daily_visible(session,market,as_of):
    if type(session) is not date or not isinstance(as_of,datetime) or as_of.tzinfo is None:raise ValueError('Explicit daily cutoff required')
    zones={'KR':'Asia/Seoul','US':'America/New_York'}
    if market not in zones:raise ValueError('Unsupported market')
    return datetime.combine(session+timedelta(days=1),time.min,ZoneInfo(zones[market])).astimezone(timezone.utc)<=as_of

def contract_price_basis(contract):
    spec=contract.specification
    params=dict(spec.get('request_parameters',{}))
    if contract.source=='kis':params.setdefault('FID_ORG_ADJ_PRC',spec.get('raw_option'))
    elif contract.source=='kis_overseas':params.setdefault('MODP',spec.get('raw_option'))
    elif contract.source=='alpaca':params.setdefault('adjustment',spec.get('adjustment'))
    endpoint=spec.get('endpoint','')
    if '{symbol}' in endpoint:endpoint=endpoint.replace('{symbol}','DECLARED')
    return classify_price_basis(contract.source,endpoint,params)
