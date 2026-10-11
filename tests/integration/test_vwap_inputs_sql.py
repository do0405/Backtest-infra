from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from sqlalchemy import select, func
import pytest
from backtest_data.catalog.models import Security
from backtest_data.ingestion.http import ProviderResponse
from backtest_data.prices.models import VWAPDailyStat

DAY = date(2020, 8, 28)
CAPTURE = datetime(2026,10,10,tzinfo=timezone.utc)


def trade(identity, price='10.125', size=4, conditions=None):
    return {'i':identity,'z':'C','x':'V','t':'2020-08-28T14:00:00.000000001Z',
            'p':Decimal(price),'s':size,'c':conditions or ['@']}


def binding(db,security):
    from backtest_data.catalog.models import IdentifierHistory
    db.add(IdentifierHistory(security_id=security.id,identifier_type='ticker',identifier='AAPL',
        venue='NASDAQ',valid_from=date(2016,1,1),available_at=datetime(2016,1,1,tzinfo=timezone.utc),
        captured_at=CAPTURE,evidence={'historical_identity_verified':True,'reference':'dated_official_identity_fixture'}))
    db.flush()


def test_sql_pages_deduplicate_and_finalize_matched_population(db_session,tmp_path):
    from backtest_data.prices.vwap_sql import create_trade_run, commit_trade_page, query_vwap_sql
    from backtest_data.prices.vwap_inputs import trade_day_request, RULE
    security=Security(market='US',source_identifier='AAPL',security_type='common_stock',name='fixture')
    db_session.add(security);db_session.flush()
    binding(db_session,security)
    run=create_trade_run(db_session,security_id=security.id,symbol='AAPL',session=DAY)
    response=ProviderResponse(b'vwap page 1',200,CAPTURE,'application/json')
    commit_trade_page(db_session,run,params=trade_day_request('AAPL',DAY),response=response,
        payload={'symbol':'AAPL','trades':[trade(1)],'next_page_token':'second'},raw_root=tmp_path)
    assert db_session.scalar(select(func.count()).select_from(VWAPDailyStat).where(VWAPDailyStat.security_id==security.id))==0
    commit_trade_page(db_session,run,params=trade_day_request('AAPL',DAY,token='second'),
        response=ProviderResponse(b'vwap page 2',200,CAPTURE+timedelta(seconds=1),'application/json'),
        payload={'symbol':'AAPL','trades':[trade(1),trade(2,price='20',size=2),trade(3,conditions=['@','I'])],
                 'next_page_token':None},raw_root=tmp_path)
    assert run.status=='complete'
    result=query_vwap_sql(db_session,security_id=security.id,sessions=[DAY],as_of=CAPTURE,
        market='US',trade_scope=RULE)
    assert result['sum_price_volume']==Decimal('80.5')
    from backtest_data.prices.numeric import fixed_divide
    assert result['eligible_volume']==6 and result['value']==fixed_divide(Decimal('80.5'),Decimal(6))
    assert result['historical_cancellation_stream_verified'] is False
    assert len(result['input_ids'])==1
    # Portable export must include every page, rather than only the final raw.
    from backtest_data.remote.registry import logical_parents
    stored=db_session.get(VWAPDailyStat,result['input_ids'][0])
    values={c.name:getattr(stored,c.name) for c in VWAPDailyStat.__table__.columns}
    parents=logical_parents({'table':'vwap_daily_stat','values':values})
    assert len(parents)==3 and sum(domain=='supplemental_document' for domain,_ in parents)==2
    with pytest.raises(ValueError,match='terminal'):
        commit_trade_page(db_session,run,params=trade_day_request('AAPL',DAY,token='second'),
            response=response,payload={'symbol':'AAPL','trades':[],'next_page_token':None},raw_root=tmp_path)


def test_sql_conflict_or_unknown_condition_prevents_adoption(db_session,tmp_path):
    from backtest_data.prices.vwap_sql import create_trade_run,commit_trade_page
    from backtest_data.prices.vwap_inputs import trade_day_request
    security=Security(market='US',source_identifier='AAPL',security_type='common_stock',name='fixture')
    db_session.add(security);db_session.flush()
    run=create_trade_run(db_session,security_id=security.id,symbol='AAPL',session=DAY)
    response=ProviderResponse(b'conflicting trades',200,CAPTURE,'application/json')
    commit_trade_page(db_session,run,params=trade_day_request('AAPL',DAY),response=response,
        payload={'symbol':'AAPL','trades':[trade(1),trade(1,price='20')],'next_page_token':None},raw_root=tmp_path)
    assert run.status=='unqualified'
    assert db_session.scalar(select(func.count()).select_from(VWAPDailyStat).where(VWAPDailyStat.security_id==security.id))==0


def test_period_and_anchor_use_missing_session_guard_and_native_units(db_session,tmp_path):
    from backtest_data.prices.vwap_sql import create_trade_run,commit_trade_page,query_vwap_sql
    from backtest_data.prices.vwap_inputs import trade_day_request,RULE
    security=Security(market='US',source_identifier='AAPL',security_type='common_stock',name='fixture')
    db_session.add(security);db_session.flush()
    binding(db_session,security)
    run=create_trade_run(db_session,security_id=security.id,symbol='AAPL',session=DAY)
    commit_trade_page(db_session,run,params=trade_day_request('AAPL',DAY),
        response=ProviderResponse(b'period trades',200,CAPTURE,'application/json'),
        payload={'symbol':'AAPL','trades':[trade(1)],'next_page_token':None},raw_root=tmp_path)
    result=query_vwap_sql(db_session,security_id=security.id,sessions=[DAY,date(2020,8,31)],
        as_of=CAPTURE,market='US',trade_scope=RULE)
    assert result['value'] is None and result['reason']=='missing_sessions'
    result=query_vwap_sql(db_session,security_id=security.id,sessions=[DAY],as_of=CAPTURE,
        market='US',trade_scope=RULE,share_basis_policy='constant_shares')
    assert result['reason']=='split_basis_evidence_required'


def test_krx_pair_requires_dated_isin_and_explicit_units(db_session,tmp_path):
    from backtest_data.prices.vwap_sql import store_krx_pair
    from backtest_data.prices.krx_quote_storage import store_krx_quotes
    from backtest_data.prices.krx_quote_models import KRXQuoteObservation
    security=Security(market='KR',source_identifier='KR7005930003',security_type='common_stock',name='fixture')
    db_session.add(security);db_session.flush()
    quote={'BAS_DD':'20200828','ISU_CD':'005930','ISU_NM':'fixture','TDD_OPNPRC':'100',
        'TDD_HGPRC':'120','TDD_LWPRC':'90','TDD_CLSPRC':'110','ACC_TRDVOL':'10','ACC_TRDVAL':'1050'}
    basic={'BAS_DD':'20200828','ISU_SRT_CD':'005930','ISU_CD':'KR7005930003','ISU_NM':'fixture'}
    for service,row in [('kospi',quote),('kospi_basic',basic)]:
        store_krx_quotes(db_session,service,DAY,ProviderResponse(service.encode(),200,CAPTURE,'application/json'),
            {'OutBlock_1':[row]},tmp_path)
    rows=db_session.scalars(select(KRXQuoteObservation)).all()
    q=next(r for r in rows if r.service=='kospi');b=next(r for r in rows if r.service=='kospi_basic')
    with pytest.raises(ValueError,match='unit'):
        store_krx_pair(db_session,quote_id=q.id,basic_id=b.id,unit_contract=None)
    result=store_krx_pair(db_session,quote_id=q.id,basic_id=b.id,
        unit_contract={'quantity_unit':'shares','notional_unit':'KRW','reference':'official_daily_field_contract'})
    assert result['sum_price_volume']==Decimal('1050') and result['eligible_volume']==10
    assert result['security_id']==security.id
    replay=store_krx_pair(db_session,quote_id=q.id,basic_id=b.id,
        unit_contract={'quantity_unit':'shares','notional_unit':'KRW','reference':'official_daily_field_contract'})
    assert replay['statistic_id']==result['statistic_id']


def test_current_ticker_only_retains_numbers_but_cannot_certify_history(db_session,tmp_path):
    from backtest_data.prices.vwap_sql import create_trade_run,commit_trade_page,query_vwap_sql
    from backtest_data.prices.vwap_inputs import trade_day_request,RULE
    security=Security(market='US',source_identifier='AAPL',security_type='common_stock',name='fixture')
    db_session.add(security);db_session.flush()
    from backtest_data.catalog.models import IdentifierHistory
    db_session.add(IdentifierHistory(security_id=security.id,identifier_type='company_name',identifier='AAPL',
        venue='NASDAQ',valid_from=date(2016,1,1),available_at=datetime(2016,1,1,tzinfo=timezone.utc),
        captured_at=CAPTURE,evidence={'historical_identity_verified':True,'reference':'verified_name_is_not_a_ticker'}))
    db_session.flush()
    run=create_trade_run(db_session,security_id=security.id,symbol='AAPL',session=DAY)
    commit_trade_page(db_session,run,params=trade_day_request('AAPL',DAY),
        response=ProviderResponse(b'identity unknown trades',200,CAPTURE,'application/json'),
        payload={'symbol':'AAPL','trades':[trade(1)],'next_page_token':None},raw_root=tmp_path)
    result=query_vwap_sql(db_session,security_id=security.id,sessions=[DAY],as_of=CAPTURE,
        market='US',trade_scope=RULE)
    assert result['value'] is None and result['reason']=='historical_identity_unverified'
    research=query_vwap_sql(db_session,security_id=security.id,sessions=[DAY],as_of=CAPTURE,
        market='US',trade_scope=RULE,allow_current_catalog=True)
    assert research['value']==Decimal('10.125') and research['historical_identity_verified'] is False


def test_archived_failed_trade_revalidation_preserves_original_capture_and_state(db_session,tmp_path):
    from backtest_data.prices.vwap_sql import create_trade_run,replay_trade_run
    from backtest_data.prices.vwap_inputs import trade_day_request
    from backtest_data.supplemental.storage import register_document
    security=Security(market='US',source_identifier='AAPL',security_type='common_stock',name='fixture')
    db_session.add(security);db_session.flush()
    old=create_trade_run(db_session,security_id=security.id,symbol='AAPL',session=DAY,
        validation_policy='all_prints_positive_v1')
    content=b'{"symbol":"AAPL","trades":[{"i":1,"z":"C","x":"V","t":"2020-08-28T14:00:00Z","p":10,"s":2,"c":["@"]},{"i":2,"z":"C","x":"V","t":"2020-08-28T20:00:00Z","p":10,"s":0,"c":["@","9"]}],"next_page_token":null}'
    doc=register_document(db_session,source='alpaca',domain='vwap_trade_pages',
        url='https://data.alpaca.markets/v2/stocks/AAPL/trades',
        request_scope={'request':trade_day_request('AAPL',DAY),'security_id':security.id},
        captured_at=CAPTURE,raw_root=tmp_path,content=content)
    old.status='unqualified';old.cursor={**old.cursor,'documents':[doc.id],'issues':['invalid_or_unknown_trade'],'page':1}
    db_session.flush();before=dict(old.cursor)
    result=replay_trade_run(db_session,old,raw_root=tmp_path)
    assert result['status']=='complete' and old.status=='unqualified' and old.cursor==before
    stat=db_session.get(VWAPDailyStat,result['statistic_id'])
    assert stat.captured_at==CAPTURE and stat.sum_price_volume==20 and stat.eligible_volume==2
    second=replay_trade_run(db_session,old,raw_root=tmp_path)
    assert second['statistic_id']==stat.id


def test_cqs_preferred_binding_requires_same_original_directory_member(db_session):
    from backtest_data.prices.vwap_sql import create_trade_run
    security=Security(market='US',source_identifier='nasdaq_directory:BIP$B',security_type='preferred_stock',name='fixture')
    db_session.add(security);db_session.flush()
    with pytest.raises(ValueError,match='binding'):
        create_trade_run(db_session,security_id=security.id,symbol='BIPpB',session=DAY)
    member={'market':'US','symbol':'BIP$B','identifier':'nasdaq_directory:BIP$B','security_id':security.id,
        'evidence':{'ACT Symbol':'BIP$B','CQS Symbol':'BIPpB'}}
    run=create_trade_run(db_session,security_id=security.id,symbol='BIPpB',session=DAY,source_member=member)
    assert run.symbol=='BIPpB' and run.security_id==security.id
