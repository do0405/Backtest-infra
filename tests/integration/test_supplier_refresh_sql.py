from contextlib import contextmanager
from datetime import date,datetime,timezone
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session
from backtest_data.db import utcnow
from backtest_data.catalog.models import Security
from backtest_data.ingestion.models import TargetUniverseManifest
from backtest_data.ingestion.http import ProviderResponse
from backtest_data.prices.supplier_models import SupplierPriceCollectionState
from backtest_data.prices.supplier_refresh_models import SupplierPriceRefreshState

def test_refresh_window_commits_separately_from_existing_backfill(db_session,tmp_path,monkeypatch):
    from backtest_data.prices import supplier_collection as mod
    stamp=datetime(2026,10,10,12,tzinfo=timezone.utc)
    sec=Security(market='US',source_identifier='refresh-fixture',security_type='common_stock',name='fixture')
    db_session.add(sec);db_session.flush()
    target=TargetUniverseManifest(content_hash='f'*64,members=[{'security_id':sec.id,'market':'US','symbol':'AAPL'}],
        scope={'output_start':'2016-01-01','output_end':'2026-10-07'},gaps=[],captured_at=stamp)
    db_session.add(target);db_session.flush()
    old=SupplierPriceCollectionState(manifest_id=target.id,source='alpaca',adjustment='split',target_hash=target.content_hash,
        target_count=1,market_target_count=1,output_start=date(2016,1,1),output_end=date(2026,10,7),
        status='collecting',cursor={'member_index':0,'page_token':'unchanged'},updated_at=stamp)
    db_session.add(old);db_session.flush();old_id=old.id
    connection=db_session.connection()
    class Borrow:
        def execute(self,*a,**kw):return connection.execute(*a,**kw)
        def commit(self):pass
    class Engine:
        @contextmanager
        def connect(self):yield Borrow()
    monkeypatch.setattr(mod,'Session',lambda _:Session(connection,join_transaction_mode='create_savepoint'))
    monkeypatch.setattr(mod,'utcnow',lambda:stamp)
    class API:
        def __init__(self,*args):pass
        def daily_prices(self,request):
            assert request.start==date(2026,10,8) and request.end==date(2026,10,9)
            return ProviderResponse(b'public adjusted update',200,stamp,'application/json'),{
                'symbol':'AAPL','next_page_token':None,'bars':[{'t':'2026-10-08T04:00:00Z','o':Decimal(10),
                    'h':Decimal(11),'l':Decimal(9),'c':Decimal(10),'v':100}]}
    monkeypatch.setattr(mod,'AlpacaData',API)
    result=mod.collect_supplier_adjusted(Engine(),None,None,tmp_path,manifest_id=target.id,
        requested_window=(date(2026,10,8),date(2026,10,9)),max_pages=1,budget_seconds=30)
    assert result['status']=='request_queue_complete' and result['progress']['inserted']==1
    db_session.expire_all()
    assert db_session.get(SupplierPriceCollectionState,old_id).cursor=={'member_index':0,'page_token':'unchanged'}
    updated=db_session.scalar(select(SupplierPriceRefreshState).where(SupplierPriceRefreshState.manifest_id==target.id))
    assert updated.output_end==date(2026,10,9) and target.scope['output_end']=='2026-10-07'
