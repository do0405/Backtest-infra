"""Daily catch-up/rolling jobs, retaining the original full target and backfill."""
from datetime import date,datetime,timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from .db import transaction
from .ingestion.models import TargetUniverseManifest
from .catalog.models import Security,Issuer,CatalogObservation
import re

def restored_mapping(fields,member,observation_id):
    """Reconstruct only a qualified, exact current master identity."""
    symbol=fields.get('provider_symbol');exchange=fields.get('exchange')
    if (fields.get('qualified_current_mapping') is not True or symbol!=member['symbol']
        or exchange not in {'NAS','NYS','AMS'} or fields.get('master_realtime_symbol')!=exchange+symbol
        or fields.get('realtime_symbol')!='D'+exchange+symbol
        or not re.fullmatch(r'[0-9a-f]{64}',str(fields.get('master_hash','')))
        or not fields.get('master_raw_id') or not fields.get('master_captured_at')):return None
    return {**fields,'source':'kis_overseas_master','security_id':member['security_id'],'market':'US',
        'catalog_observation_id':observation_id,'match_rule':'exact_symbol_unique_across_complete_us_masters'}

SOURCES={'kis','kis_overseas','alpaca','sec','opendart'}

def update_window(as_of,market,*,since=None,lookback_days=14):
    if not isinstance(as_of,datetime) or as_of.tzinfo is None:raise ValueError('Aware refresh cutoff required')
    zone={'KR':'Asia/Seoul','US':'America/New_York'}[market]
    end=(as_of-timedelta(minutes=16 if market=='US' else 0)).astimezone(ZoneInfo(zone)).date()-timedelta(days=1)
    while end.weekday()>4:end-=timedelta(days=1)
    start=end-timedelta(days=lookback_days-1)
    if since is not None:
        if type(since) is not date:raise ValueError('Exact catch-up date required')
        start=min(start,since+timedelta(days=1))
    return start,end

def build_update_jobs(members,issuers,*,manifest_id,original_scope,as_of,enabled_sources=None,since=None,mappings=None):
    enabled=SOURCES if enabled_sources is None else set(enabled_sources)
    if enabled-SOURCES:raise ValueError('Unregistered update source')
    mappings=mappings or {}; jobs=[];total=len(members)
    if not total:raise ValueError('Full target required')
    since=since if since is not None else date.fromisoformat(original_scope['output_end'])
    for member in members:
        market=member['market'];start,end=update_window(as_of,market,since=since)
        request={'market':market,'symbol':member['symbol'],'manifest_id':manifest_id,'target_count':total,
            'original_backfill_start':original_scope['output_start'],'original_backfill_end':original_scope['output_end'],
            'output_start':max(date(2016,1,1),start).isoformat(),'output_end':end.isoformat(),
            'refresh_policy':'catchup_and_rolling14_calendar_days_v1'}
        sources=('kis',) if market=='KR' else ('alpaca','kis_overseas')
        for source in sources:
            if source not in enabled:continue
            specific=dict(request)
            if source=='alpaca':specific['page_size']=100
            if source=='kis_overseas':
                proof=mappings.get(member['security_id'])
                specific['mapping_status']='qualified' if proof else 'unsupported'
                if proof:specific.update(provider_symbol=proof['provider_symbol'],exchange=proof['exchange'],mapping_evidence=proof)
                else:specific['mapping_reason']='restored_exact_master_mapping_unavailable'
            jobs.append({'source':source,'domain':'prices','entity_id':member['security_id'],'request':specific,'priority':1})
    for issuer in issuers:
        source='opendart' if issuer['market']=='KR' else 'sec'
        if source not in enabled:continue
        start,end=update_window(as_of,issuer['market'],since=since,lookback_days=90)
        request={'market':issuer['market'],'issuer_id':issuer['id'],'manifest_id':manifest_id,'target_count':total,
            'output_start':original_scope['output_start'],'output_end':end.isoformat(),
            'discovery_start':max(date(1999,1,1),start).isoformat(),'refresh_policy':'new_and_corrected_filings_rolling90_days_v1',
            'original_backfill_start':original_scope['output_start'],'original_backfill_end':original_scope['output_end']}
        jobs.append({'source':source,'domain':'issuer_discovery','entity_id':issuer['id'],'request':request,'priority':1})
    return jobs

def plan_remote_jobs(engine,manifest_id,as_of,*,enabled_sources=None,since=None):
    with transaction(engine) as session:
        target=session.get(TargetUniverseManifest,manifest_id)
        if target is None:raise ValueError('Original manifest missing')
        members=list(target.members);scope=dict(target.scope)
        ids={m['security_id'] for m in members}
        securities=session.scalars(select(Security).where(Security.id.in_(ids))).all()
        if len(securities)!=len(ids):raise ValueError('Target bootstrap security missing')
        issuer_ids={s.issuer_id for s in securities if s.issuer_id}
        issuer_ids.update(m['issuer_id'] for m in members if m.get('issuer_id'))
        issuers=[{'id':i.id,'market':i.market} for i in session.scalars(select(Issuer).where(Issuer.id.in_(issuer_ids)))]
        identifier_to_id={s.source_identifier:s.id for s in securities};mappings={}
        member_by_id={m['security_id']:m for m in members}
        if enabled_sources is None or 'kis_overseas' in enabled_sources:
            records=session.scalars(select(CatalogObservation).where(CatalogObservation.source=='kis_overseas_master',
                CatalogObservation.identifier.in_(identifier_to_id)).order_by(CatalogObservation.captured_at.desc()))
            for observation in records:
                fields=observation.fields or {}
                identity=identifier_to_id[observation.identifier]
                proof=restored_mapping(fields,member_by_id[identity],observation.id)
                if proof:mappings.setdefault(identity,proof)
    return build_update_jobs(members,issuers,manifest_id=manifest_id,original_scope=scope,as_of=as_of,
        enabled_sources=enabled_sources,since=since,mappings=mappings)
