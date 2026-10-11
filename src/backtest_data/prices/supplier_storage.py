"""Preserve API-declared adjusted bars without requiring an action ledger."""
from datetime import datetime
from decimal import Decimal,DecimalException
from collections.abc import Mapping
from urllib.parse import unquote
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from ..catalog.sources import ensure_contract
from ..db import new_id,utcnow
from ..ingestion.raw_store import RawStore
from ..ingestion.models import RawObject,IngestionAttempt
from ..ingestion.jobs import stable_hash
from ..releases.models import SelectionDecision
from .models import PriceObservation
from .provider_basis import classify_price_basis,supplier_daily_visible

def exact_number(value):
    if value is None or value=='':return None
    if isinstance(value,(float,bool)):raise ValueError('Exact source numeric required')
    result=Decimal(value)
    if not result.is_finite() or result<0:raise ValueError('Invalid source numeric')
    return result

def decode_supplier_bar(source,row):
    if source=='alpaca':
        from .adapters.alpaca import new_york_session
        day=new_york_session(datetime.fromisoformat(str(row['t']).replace('Z','+00:00')))
        names=('o','h','l','c','v');amount=None
    elif source=='kis':
        day=datetime.strptime(row['stck_bsop_date'],'%Y%m%d').date()
        names=('stck_oprc','stck_hgpr','stck_lwpr','stck_clpr','acml_vol');amount=exact_number(row.get('acml_tr_pbmn'))
    elif source=='kis_overseas':
        day=datetime.strptime(row['xymd'],'%Y%m%d').date()
        names=('open','high','low','clos','tvol');amount=exact_number(row.get('tamt'))
    else:raise ValueError('Unsupported supplier')
    values=[exact_number(row.get(name)) for name in names]
    o,h,l,c,v=values
    if any(x is None or x<=0 for x in (o,h,l,c)) or not l<=min(o,c)<=max(o,c)<=h:raise ValueError('Invalid OHLC')
    return day,values,amount

def store_supplier_prices(session,*,security_id,source,market,endpoint,params,response,payload,raw_root,output_start=None,output_end=None):
    definition=classify_price_basis(source,endpoint,params)
    if definition['basis']!='provider_adjusted':raise ValueError('Separate supplier adjusted input required')
    if market!=('KR' if source=='kis' else 'US'):raise ValueError('Unsupported supplier market')
    if source=='alpaca' and (params.get('feed')!='sip' or params.get('currency')!='USD'):
        raise ValueError('Supplier collection requires declared SIP and USD')
    field='bars' if source=='alpaca' else 'output2'
    if field not in payload or not isinstance(payload[field],list):raise ValueError('Explicit supplier bar array required')
    # Contract contains adjustment parameters, never symbol/date/auth fields.
    flagname={'kis':'FID_ORG_ADJ_PRC','kis_overseas':'MODP','alpaca':'adjustment'}[source]
    spec={**definition,'request_parameters':{flagname:params[flagname]},'qualification':'supplier_declared_research',
        'price_unit':'currency_per_share','quantity_unit':'shares','matched_vwap':False,
        'adjustment_reference_policy':'provider_current_basis_not_historical_action_certificate'}
    if source=='alpaca':spec.update(feed=params.get('feed'),currency=params.get('currency'),endpoint='/v2/stocks/{symbol}/bars')
    contract=ensure_contract(session,source=source,domain='prices',market=market,specification=spec)
    raw=RawStore(raw_root).put(response.content)
    session.execute(insert(RawObject).values(id=new_id(),content_hash=raw.content_hash,storage_key=raw.storage_key,byte_count=raw.byte_count).on_conflict_do_nothing(index_elements=[RawObject.content_hash]))
    rawrow=session.scalar(select(RawObject).where(RawObject.content_hash==raw.content_hash))
    public_params={k:v for k,v in params.items() if not any(t in k.lower() for t in ('auth','key','secret','token'))}
    session.add(IngestionAttempt(source_contract_id=contract.id,request_hash=stable_hash(public_params),content_hash=raw.content_hash,
        status='received',captured_at=response.captured_at,evidence={'endpoint':endpoint,'params':public_params,'price_basis_policy':definition}))
    rows=payload[field]
    if source=='alpaca' and payload.get('symbol')!=unquote(endpoint.split('/')[3]):raise ValueError('Supplier symbol mismatch')
    if not isinstance(rows,list):raise ValueError('Invalid supplier page')
    inserted=0;rejected=0;days=[];records=[]
    for row in rows:
        try:
            day,values,amount=decode_supplier_bar(source,row)
            if not supplier_daily_visible(day,market,response.captured_at):raise ValueError('Unclosed source session')
            if output_start and day<output_start or output_end and day>output_end:raise ValueError('Source session outside request')
        except (ValueError,KeyError,TypeError,DecimalException):rejected+=1;continue
        days.append(day)
        o,h,l,c,v=values
        signature=stable_hash(['supplier_adjusted_capture_v1',security_id,contract.id,str(day),[str(x) for x in values],response.captured_at.isoformat()])
        record_id=new_id()
        records.append(dict(id=record_id,security_id=security_id,source_contract_id=contract.id,raw_id=rawrow.id,
            session=day,basis='provider_adjusted',feed='sip' if source=='alpaca' else source,venue='US_CONSOLIDATED' if market=='US' else 'KRX',
            trade_scope='supplier_daily_eligibility_unverified',currency='USD' if market=='US' else 'KRW',quantity_unit='shares',
            open=o,high=h,low=l,close=c,volume=v,notional=None,signature=signature,available_at=response.captured_at,captured_at=response.captured_at,
            evidence={'price_basis_policy':definition,'basis_reference_captured_at':response.captured_at.isoformat(),
                'raw_turnover':str(amount) if amount is not None else None,'turnover_matches_adjusted_volume':None,
                'future_trade_dates_excluded':True,'corporate_action_role':'supplemental','historical_adjustment_reference_verified':False}))
    for offset in range(0,len(records),100):
        ids=session.execute(insert(PriceObservation).values(records[offset:offset+100])
            .on_conflict_do_nothing(index_elements=[PriceObservation.signature]).returning(PriceObservation.id)).scalars().all()
        inserted+=len(ids)
        if ids:session.execute(insert(SelectionDecision).values([{'id':new_id(),'record_domain':'price_observation','record_id':identity,
            'status':'accepted','rule_version':'provider_declared_price_structure_v1','evidence_grade':'supplier_declared',
            'reason':'API adjustment flag and structural OHLC; action history supplemental','actor':'collector','recorded_at':utcnow()} for identity in ids]))
    return {'source_rows':len(rows),'inserted':inserted,'rejected':rejected,'oldest_session':min(days).isoformat() if days else None,
        'source_contract_id':contract.id,'basis':definition,'coverage_complete':False}
