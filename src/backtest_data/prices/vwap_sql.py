"""SQL connection for immutable matched N/D statistics and resumable trades."""
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert
from ..catalog.models import Security, IdentifierHistory
from ..catalog.sources import ensure_contract
from ..db import new_id, utcnow
from ..ingestion.jobs import stable_hash
from ..releases.models import SelectionDecision
from ..supplemental.storage import register_document
from .models import VWAPDailyStat
from .vwap_input_models import VWAPTradeRun, VWAPTradeEvent
from .vwap_inputs import RULE, IDENTITY_POLICY, INPUT_POLICY, REFERENCE, normalize_trade_page, trade_day_request, trade_day_bounds
from .numeric import fixed_divide, exact_sum, numeric_signature
from .provider_basis import supplier_daily_visible


def store_krx_pair(db, *, quote_id, basic_id, unit_contract):
    """Use a same-date official ISIN row and an explicitly reviewed unit pair.

    No historical short code is attached to today's identity without dated
    basic information. The scope remains the provider's reported daily pair;
    it does not assert regular-hours or KRX/NXT consolidated coverage.
    """
    from .krx_quote_models import KRXQuoteObservation
    from zoneinfo import ZoneInfo
    from datetime import time,timedelta
    if (not isinstance(unit_contract,dict) or unit_contract.get('quantity_unit')!='shares'
            or unit_contract.get('notional_unit')!='KRW' or not unit_contract.get('reference')):
        raise ValueError('explicit_KRX_unit_contract_required')
    quote,basic=db.get(KRXQuoteObservation,quote_id),db.get(KRXQuoteObservation,basic_id)
    if (quote is None or basic is None or quote.record_kind!='daily_quote' or basic.record_kind!='basic_information'
            or quote.reported_date is None or quote.reported_date!=quote.request_date
            or basic.reported_date!=quote.reported_date or basic.request_date!=quote.request_date
            or basic.source_code!=quote.source_code or not basic.source_isin
            or quote.service not in {'kospi','kosdaq'} or basic.service!=quote.service+'_basic'):
        raise ValueError('dated_KRX_ISIN_binding_required')
    security=db.scalar(select(Security).where(Security.market=='KR',Security.source_identifier==basic.source_isin))
    if security is None:raise ValueError('KRX_ISIN_outside_current_target_catalog')
    if not supplier_daily_visible(quote.reported_date,'KR',quote.captured_at):
        raise ValueError('completed_KRX_session_required')
    n,v=quote.notional,quote.volume
    if n is None or v is None or n<0 or v<0 or not n.is_finite() or not v.is_finite() or (v==0 and n!=0):
        raise ValueError('matched_KRX_statistics_required')
    reference=stable_hash(['krx_reported_daily_pair_v1',quote_id,basic_id,unit_contract])
    # A deterministic provenance key prevents re-materializing the same capture.
    from uuid import uuid5,NAMESPACE_URL
    identity=str(uuid5(NAMESPACE_URL,'backtest-vwap:'+reference))
    previous=db.get(VWAPDailyStat,identity)
    if previous is not None:return {'statistic_id':identity,'security_id':security.id,
        'sum_price_volume':previous.sum_price_volume,'eligible_volume':previous.eligible_volume,'replayed':True}
    contract=ensure_contract(db,source='krx',domain='vwap',market='KR',specification={
        'rule':'krx_reported_daily_pair_v1','unit_contract':unit_contract,'quote_service':quote.service,
        'trade_scope':'provider_reported_daily_totals','session_market_scope_verified':False,
        'basis':'as_traded','currency':'KRW','quantity_unit':'shares'})
    stat=VWAPDailyStat(id=identity,security_id=security.id,source_contract_id=contract.id,raw_id=quote.raw_id,
        session=quote.reported_date,sum_price_volume=n,eligible_volume=v,
        trade_scope='krx_reported_daily_pair_v1',method='matched_daily_statistics',basis='as_traded',
        available_at=datetime.combine(quote.reported_date+timedelta(days=1),time(),ZoneInfo('Asia/Seoul')),
        captured_at=max(quote.captured_at,basic.captured_at),materialized_at=utcnow(),
        evidence={'page_chain_complete':True,'krx_quote_ids':[quote_id,basic_id],
            'same_response_pair':True,'source_fields':['ACC_TRDVAL','ACC_TRDVOL'],
            'unit_contract':unit_contract,'session_market_scope_verified':False,
            'share_basis_policy':'native_trade_units','identity_source':'same_date_official_basic_ISIN',
            'no_trade_evidence':v==0})
    db.add(stat);db.flush()
    db.add(SelectionDecision(record_domain='vwap_daily_stat',record_id=identity,status='accepted',
        rule_version='matched_KRX_reported_pair_v1',evidence_grade='supplier_reported_matched_pair',
        reason='Same-response daily N/D with dated ISIN; trade-market scope remains provider reported',
        actor='collector',recorded_at=utcnow()))
    return {'statistic_id':identity,'security_id':security.id,'sum_price_volume':n,'eligible_volume':v,'replayed':False}


def create_trade_run(db, *, security_id, symbol, session, limit=10000, validation_policy=INPUT_POLICY,source_member=None):
    security = db.get(Security, security_id)
    if security is None or security.market != 'US':
        raise ValueError('US_security_required')
    if security.source_identifier not in {symbol,'nasdaq_directory:'+symbol}:
        from .vwap_inputs import source_trade_symbol
        if (source_member is None or source_member.get('security_id')!=security_id
                or source_member.get('identifier')!=security.source_identifier
                or source_trade_symbol(source_member)!=symbol):
            raise ValueError('literal_catalog_symbol_binding_required')
    params = trade_day_request(symbol, session, limit=limit)
    if validation_policy not in {INPUT_POLICY,'all_prints_positive_v1'}:raise ValueError('known_trade_validation_policy_required')
    key = stable_hash([RULE, IDENTITY_POLICY, validation_policy, security_id, symbol, params])
    db.execute(insert(VWAPTradeRun).values(id=new_id(),security_id=security_id,symbol=symbol,
        session=session,rule=RULE,request_hash=key,status='pending',
        cursor={'page':0,'next_page_token':None,'documents':[],'issues':[], 'limit':limit,
            'validation_policy':validation_policy,'source_member':source_member},
        updated_at=utcnow()).on_conflict_do_nothing(index_elements=[VWAPTradeRun.request_hash]))
    return db.scalar(select(VWAPTradeRun).where(VWAPTradeRun.request_hash==key).with_for_update())


def commit_trade_page(db, run, *, params, response, payload, raw_root, revalidation_of=None):
    if run.status in {'complete','unqualified'}:
        raise ValueError('terminal_trade_run')
    if not supplier_daily_visible(run.session,'US',response.captured_at):
        raise ValueError('completed_trade_session_required')
    state = dict(run.cursor)
    expected = trade_day_request(run.symbol,run.session,token=state['next_page_token'],limit=state['limit'])
    if params != expected:
        raise ValueError('trade_request_anchor_or_page_chain_mismatch')
    endpoint = 'https://data.alpaca.markets/v2/stocks/'+run.symbol+'/trades'
    document = register_document(db,source='alpaca',domain='vwap_trade_pages',url=endpoint,
        request_scope={'request':params,'security_id':run.security_id,'rule':RULE,
            'session':run.session.isoformat(),'page':state['page'],'request_hash':run.request_hash,
            'revalidation_of_document_id':revalidation_of},
        captured_at=response.captured_at,raw_root=raw_root,content=response.content)
    try:events,issues=normalize_trade_page(payload,symbol=run.symbol,session=run.session)
    except (ValueError,TypeError):events,issues=[],['invalid_trade_page']
    token=payload.get('next_page_token') if isinstance(payload,dict) else None
    if token is not None and (not isinstance(token,str) or not token):
        token=None;issues.append('invalid_next_page_token')
    if token is not None and stable_hash(token) in state.get('token_hashes',[]):
        issues.append('page_token_cycle')
    # SQL identity deduplication persists across pages and process restarts.
    # A conflicting source event remains in the raw document and blocks adoption.
    first = {}
    for event in events:
        previous = first.get(event['event_key'])
        if previous is not None and previous['fingerprint'] != event['fingerprint']:
            issues.append('conflicting_trade_identity')
        first.setdefault(event['event_key'],event)
    keys = list(first)
    for offset in range(0,len(keys),250):
        chunk = keys[offset:offset+250]
        prior = dict(db.execute(select(VWAPTradeEvent.event_key,VWAPTradeEvent.fingerprint)
            .where(VWAPTradeEvent.run_id==run.id,VWAPTradeEvent.event_key.in_(chunk))).all())
        records = []
        for key in chunk:
            event = first[key]
            if key in prior:
                if prior[key]!=event['fingerprint']:issues.append('conflicting_trade_identity')
            else:records.append(dict(id=new_id(),run_id=run.id,document_id=document.id,**event))
        if records:db.execute(insert(VWAPTradeEvent).values(records))
    if events:
        stamp=(events[0]['timestamp_second'].isoformat(),events[0]['timestamp_nanosecond'])
        if state.get('last_timestamp') and tuple(state['last_timestamp']) > stamp:
            issues.append('nonmonotonic_page_chain')
        state['last_timestamp']=[events[-1]['timestamp_second'].isoformat(),events[-1]['timestamp_nanosecond']]
    state['documents']=[*state['documents'],document.id]
    state['issues']=list(dict.fromkeys([*state['issues'],*issues]))
    state['page']+=1
    state['next_page_token']=token
    state['token_hashes']=[*state.get('token_hashes',[]),stable_hash(token)] if token else state.get('token_hashes',[])
    run.cursor=state;run.updated_at=utcnow();run.status='collecting'
    if token is None or 'page_token_cycle' in state['issues']:
        run.status='unqualified' if state['issues'] else 'complete'
        if run.status=='complete':
            numerator, volume, count = db.execute(select(func.coalesce(func.sum(VWAPTradeEvent.numerator),0),
                func.coalesce(func.sum(VWAPTradeEvent.quantity),0),func.count())
                .where(VWAPTradeEvent.run_id==run.id,VWAPTradeEvent.eligible.is_(True))).one()
            numerator,volume=Decimal(numerator),Decimal(volume)
            contract=ensure_contract(db,source='alpaca',domain='vwap',market='US',specification={
                'rule':RULE,'reference':REFERENCE,'feed':'sip','currency':'USD','quantity_unit':'shares',
                'basis':'as_traded','trade_scope':'full_new_york_calendar_day',
                'eligibility':'daily_high_low_AND_volume','asof':'-',
                'historical_rule_versions_verified':False,'historical_cancellation_stream_verified':False,
                'trade_identity_policy':IDENTITY_POLICY})
            bindings=db.scalars(select(IdentifierHistory).where(IdentifierHistory.security_id==run.security_id,
                IdentifierHistory.identifier_type=='ticker',
                IdentifierHistory.identifier==run.symbol,IdentifierHistory.valid_from<=run.session,
                (IdentifierHistory.valid_to.is_(None)) | (IdentifierHistory.valid_to>=run.session),
                IdentifierHistory.available_at<=trade_day_bounds(run.session)[1])).all()
            binding_ids=[row.id for row in bindings if (row.evidence or {}).get('historical_identity_verified') is True
                and (row.evidence or {}).get('reference')]
            identity=new_id()
            stat=VWAPDailyStat(id=identity,security_id=run.security_id,source_contract_id=contract.id,
                raw_id=document.raw_id,session=run.session,sum_price_volume=numerator,eligible_volume=volume,
                trade_scope=RULE,method='trade_aggregated',basis='as_traded',
                available_at=trade_day_bounds(run.session)[1],captured_at=response.captured_at,
                materialized_at=utcnow(),evidence={'rule':RULE,'request_hash':run.request_hash,
                    'page_document_ids':state['documents'],'eligible_trade_count':count,'page_count':state['page'],
                    'population':'historical_REST_returned_trades','page_chain_complete':True,
                    'numeric_signature':numeric_signature(numerator,volume,run.request_hash),
                    'historical_cancellation_stream_verified':False,'historical_rule_versions_verified':False,
                    'identity_policy':'literal_symbol_no_automatic_rename_mapping','trade_identity_policy':IDENTITY_POLICY,
                    'source_symbol':run.symbol,'input_validation_policy':state.get('validation_policy','all_prints_positive_v1'),
                    'no_trade_evidence':False,'zero_population_reason':'no_eligible_trade_returned' if volume==0 else None})
            stat.evidence={**stat.evidence,'historical_identity_verified':bool(binding_ids),
                'identifier_history_ids':binding_ids,'canonical_identity_policy':'explicit_dated_verified_ticker_binding'}
            db.add(stat);db.flush()
            db.add(SelectionDecision(record_domain='vwap_daily_stat',record_id=identity,status='accepted' if binding_ids else 'provisional',
                rule_version='matched_trade_population_v1',evidence_grade='exact_returned_trade_scope',
                reason='Complete pages; matched Decimal numerator and eligible volume; declared daily rule',
                actor='collector',recorded_at=utcnow()))
            run.cursor={**state,'statistic_id':identity,'sum_price_volume':str(numerator),'eligible_volume':str(volume)}
    db.flush()
    return {'run_id':run.id,'status':run.status,'pages':state['page'],'issues':state['issues'],
            'statistic_id':run.cursor.get('statistic_id'),'coverage_complete':False}


def replay_trade_run(db, original, *, raw_root):
    """Revalidate archived captures with new policy, never modifying old state."""
    import hashlib
    from ..supplemental.models import SupplementalDocument
    from ..ingestion.models import RawObject
    from ..ingestion.raw_store import RawStore
    from ..ingestion.http import ProviderResponse
    from ..supplemental.alpaca_inputs import decode_alpaca_json
    if original.status!='unqualified':raise ValueError('only_failed_terminal_trade_runs_can_revalidate')
    run=create_trade_run(db,security_id=original.security_id,symbol=original.symbol,session=original.session,
        limit=original.cursor['limit'],validation_policy=INPUT_POLICY,source_member=original.cursor.get('source_member'))
    if run.id==original.id:raise ValueError('same_validation_policy_cannot_reinterpret_history')
    if run.status in {'complete','unqualified'}:
        return {'status':run.status,'run_id':run.id,'statistic_id':run.cursor.get('statistic_id'),'replayed':True}
    original_docs=original.cursor['documents']
    if not original_docs:raise ValueError('original_trade_documents_missing')
    for identity in original_docs[run.cursor['page']:]:
        doc=db.get(SupplementalDocument,identity);raw=db.get(RawObject,doc.raw_id)
        params=trade_day_request(run.symbol,run.session,token=run.cursor['next_page_token'],limit=run.cursor['limit'])
        expected={k:v for k,v in params.items() if k!='page_token'}
        request=doc.request_scope.get('request',{})
        if (doc.source!='alpaca' or doc.domain!='vwap_trade_pages'
                or doc.source_url!='https://data.alpaca.markets/v2/stocks/'+run.symbol+'/trades'
                or doc.request_scope.get('security_id')!=run.security_id
                or {k:v for k,v in request.items() if k!='page_token'}!=expected):
            raise ValueError('original_trade_capture_anchor_mismatch')
        body=RawStore(raw_root).read(raw.storage_key)
        if hashlib.sha256(body).hexdigest()!=raw.content_hash or doc.content_hash!=raw.content_hash:
            raise ValueError('original_trade_capture_hash_mismatch')
        result=commit_trade_page(db,run,params=params,
            response=ProviderResponse(body,200,doc.captured_at,'application/json'),
            payload=decode_alpaca_json(body),raw_root=raw_root,revalidation_of=identity)
    return {**result,'original_run_id':original.id,'replayed':False}


def validate_vwap_window(sessions,as_of,market,anchor_known_at=None):
    if not sessions or sorted(set(sessions))!=list(sessions):
        raise ValueError('ordered_unique_session_grid_required')
    if as_of.tzinfo is None or not all(supplier_daily_visible(day,market,min(as_of,utcnow())) for day in sessions):
        raise ValueError('completed_sessions_required')
    if anchor_known_at is not None and (anchor_known_at.tzinfo is None or anchor_known_at>min(as_of,utcnow())):
        raise ValueError('anchor_not_known')


def query_vwap_sql(db, *, security_id, sessions, as_of, market, trade_scope,
                   anchor_known_at=None, share_basis_policy='native_trade_units', statistic_ids=None,
                   allow_current_catalog=False):
    """Exact raw-unit period/Anchored VWAP, without inventing split coverage.

    Provide an explicit session grid: missing inputs cannot silently shrink a
    rolling window. Optional frozen statistic IDs make this release/replay safe.
    The native-unit sum is not a constant-share-basis, split-adjusted VWAP.
    """
    validate_vwap_window(sessions,as_of,market,anchor_known_at)
    if type(allow_current_catalog) is not bool:raise ValueError('explicit_research_identity_policy_required')
    if db.get(Security,security_id) is None or db.get(Security,security_id).market!=market:
        raise ValueError('security_market_mismatch')
    if share_basis_policy not in {'native_trade_units','constant_shares'}:
        raise ValueError('unknown_share_basis_policy')
    if share_basis_policy=='constant_shares':
        return {'value':None,'reason':'split_basis_evidence_required','input_ids':[]}
    query=select(VWAPDailyStat).where(VWAPDailyStat.security_id==security_id,
        VWAPDailyStat.session.in_(sessions),VWAPDailyStat.trade_scope==trade_scope,VWAPDailyStat.basis=='as_traded')
    if statistic_ids is not None:query=query.where(VWAPDailyStat.id.in_(statistic_ids))
    candidates=db.scalars(query).all()
    selected={};identity_missing=set()
    for row in candidates:
        if (row.method not in {'trade_aggregated','matched_daily_statistics'}
                or not row.evidence.get('page_chain_complete') or row.available_at > as_of):
            continue
        decisions=db.scalars(select(SelectionDecision).where(SelectionDecision.record_domain=='vwap_daily_stat',
            SelectionDecision.record_id==row.id).order_by(SelectionDecision.recorded_at.desc(),SelectionDecision.id.desc())).all()
        identity_verified=row.evidence.get('historical_identity_verified') is True or row.evidence.get('identity_source')=='same_date_official_basic_ISIN'
        if not identity_verified and not allow_current_catalog:
            identity_missing.add(row.session);continue
        if not decisions or decisions[0].status not in ({'accepted','provisional'} if allow_current_catalog else {'accepted'}):continue
        old=selected.get(row.session)
        if old is None or (row.captured_at,row.id)>(old.captured_at,old.id):selected[row.session]=row
    missing=[day.isoformat() for day in sessions if day not in selected]
    if missing:return {'value':None,'reason':'historical_identity_unverified' if identity_missing else 'missing_sessions',
        'missing_sessions':missing,'input_ids':[]}
    rows=[selected[day] for day in sessions]
    if len({r.source_contract_id for r in rows})!=1:
        return {'value':None,'reason':'incompatible_source_contracts','input_ids':[]}
    numerator=exact_sum(r.sum_price_volume for r in rows)
    volume=exact_sum(r.eligible_volume for r in rows)
    return {'value':fixed_divide(numerator,volume) if volume else None,'sum_price_volume':numerator,
        'eligible_volume':volume,'reason':None if volume else 'zero_eligible_volume',
        'input_ids':[r.id for r in rows],'page_document_ids':[identity for r in rows for identity in r.evidence.get('page_document_ids',[])],
        'sessions':[day.isoformat() for day in sessions],'trade_scope':trade_scope,
        'share_basis_policy':share_basis_policy,'price_capture_pit_required':False,
        'historical_identity_verified':all(r.evidence.get('historical_identity_verified') is True
            or r.evidence.get('identity_source')=='same_date_official_basic_ISIN' for r in rows),
        'identity_policy':'current_catalog_research' if allow_current_catalog else 'verified_dated_identity',
        'historical_cancellation_stream_verified':False,'historical_rule_versions_verified':False,
        'input_signature':numeric_signature([r.id for r in rows],numerator,volume),
        'coverage_complete':False}
