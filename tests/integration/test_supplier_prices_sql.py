from datetime import datetime,timezone
from decimal import Decimal
from sqlalchemy import select
from backtest_data.catalog.models import Security
from backtest_data.prices.models import PriceObservation
from backtest_data.ingestion.http import ProviderResponse

def test_supplier_adjusted_sql_classification_and_replay(db_session,tmp_path):
    from backtest_data.prices.supplier_storage import store_supplier_prices
    security=Security(market='US',source_identifier='supplier-basis-fixture',security_type='common_stock',name='fixture')
    db_session.add(security);db_session.flush()
    response=ProviderResponse(b'fixture adjusted bars',200,datetime(2026,10,10,tzinfo=timezone.utc),'application/json')
    kwargs=dict(security_id=security.id,source='alpaca',market='US',endpoint='/v2/stocks/AAPL/bars',
        params={'adjustment':'split','feed':'sip','currency':'USD','requested_symbol':'AAPL'},response=response,
        payload={'symbol':'AAPL','bars':[{'t':'2016-01-04T05:00:00Z','o':Decimal('25'),'h':Decimal('26'),'l':Decimal('24'),'c':Decimal('25.5'),'v':400}]},raw_root=tmp_path)
    assert store_supplier_prices(db_session,**kwargs)['inserted']==1
    assert store_supplier_prices(db_session,**kwargs)['inserted']==0
    row=db_session.scalar(select(PriceObservation).where(PriceObservation.security_id==security.id))
    assert row.basis=='provider_adjusted' and row.close==Decimal('25.5')
    assert row.evidence['price_basis_policy']['adjustments']==['split']
    assert not row.evidence['price_basis_policy']['corporate_action_completeness_required']
    assert row.notional is None
    import pytest
    with pytest.raises(ValueError,match='bar array'):
        with db_session.begin_nested():store_supplier_prices(db_session,**{**kwargs,'payload':{'symbol':'AAPL'}})
    with pytest.raises(ValueError,match='SIP'):
        with db_session.begin_nested():store_supplier_prices(db_session,**{**kwargs,'params':{**kwargs['params'],'feed':'iex'}})
    with db_session.begin_nested():
        broken={**kwargs['payload']['bars'][0],'o':'bad-number'}
        assert store_supplier_prices(db_session,**{**kwargs,'payload':{'symbol':'AAPL','bars':[broken]}})['rejected']==1

def test_supplier_page_rollback_does_not_advance_sql_continuation(db_session,tmp_path):
    from datetime import date
    from types import SimpleNamespace
    from sqlalchemy import func
    import pytest
    from backtest_data.prices.supplier_collection import SupplierAlpacaRequest,commit_supplier_page
    security=Security(market='US',source_identifier='supplier-page-fixture',security_type='common_stock',name='fixture')
    db_session.add(security);db_session.flush()
    stamp=datetime(2026,10,10,tzinfo=timezone.utc)
    request=SupplierAlpacaRequest(date(2016,1,1),date(2026,10,7),'AAPL','split','old-page',stamp)
    state=SimpleNamespace(cursor={'member_index':0,'pages':0},updated_at=stamp,status='ready')
    response=ProviderResponse(b'fixture page',200,stamp,'application/json')
    bars=[{'t':'2016-01-04T05:00:00Z','o':Decimal('25'),'h':Decimal('26'),'l':Decimal('24'),'c':Decimal('25.5'),'v':400}]
    with pytest.raises(ValueError,match='Repeated supplier'):
        with db_session.begin_nested():
            commit_supplier_page(db_session,state,request=request,response=response,
                payload={'symbol':'AAPL','bars':bars,'next_page_token':'old-page'},raw_root=tmp_path,member={'security_id':security.id})
    assert state.cursor=={'member_index':0,'pages':0}
    assert db_session.scalar(select(func.count()).select_from(PriceObservation).where(PriceObservation.security_id==security.id))==0
    with db_session.begin_nested():
        commit_supplier_page(db_session,state,request=request,response=response,
            payload={'symbol':'AAPL','bars':bars,'next_page_token':'next-page'},raw_root=tmp_path,member={'security_id':security.id})
    assert state.cursor['page_token']=='next-page' and state.cursor['pages']==1
