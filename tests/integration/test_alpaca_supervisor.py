from contextlib import contextmanager
import json
from types import SimpleNamespace

from sqlalchemy import func, select

from backtest_data.catalog.models import Security
from backtest_data.db import utcnow
from backtest_data.ingestion.http import ProviderError, ProviderResponse
from backtest_data.ingestion.jobs import plan_job
from backtest_data.ingestion.supervisor import run_collection
from backtest_data.prices.models import PriceObservation, VWAPDailyStat


def harness(session,monkeypatch,client):
    import backtest_data.ingestion.supervisor as module
    @contextmanager
    def tx(engine):
        yield session
    monkeypatch.setattr(module,'transaction',tx)
    monkeypatch.setattr(module,'ProviderHTTP',lambda engine:SimpleNamespace(close=lambda:None))
    monkeypatch.setattr(module,'load_credentials',lambda **kwargs:SimpleNamespace(alpaca_key=True,alpaca_secret=True))
    monkeypatch.setattr(module,'AlpacaData',lambda *args:client,raising=False)
    security=Security(market='US',source_identifier='nasdaq_directory:AAPL',security_type='common_stock',name='Apple')
    session.add(security);session.flush()
    job=plan_job(session,source='alpaca',domain='prices',entity_id=security.id,
        request={'market':'US','symbol':'AAPL','output_start':'2020-01-01','output_end':'2020-01-03'})
    return module,job


def response(bars,token=None):
    data={'symbol':'AAPL','bars':bars,'next_page_token':token}
    return ProviderResponse(json.dumps(data).encode(),200,utcnow(),'application/json'),data


def bar(value='100'):
    return {'t':'2020-01-02T05:00:00Z','o':value,'h':value,'l':value,'c':value,'v':10,'vw':value}


def test_free_sip_pagination_uses_stable_request_anchor_and_stores_only_raw_prices(db_session,monkeypatch,tmp_path):
    seen=[]
    class Client:
        def daily_prices(self,request):
            seen.append(request)
            return response([bar()], 'page2') if len(seen)==1 else response([])
    _,job=harness(db_session,monkeypatch,Client())
    result=run_collection(None,tmp_path,market='us',domain='prices')
    db_session.refresh(job)
    assert len(seen)==2 and seen[1].page_token=='page2'
    assert seen[1].captured_at==seen[0].captured_at
    assert seen[0].page_size==seen[1].page_size==100
    assert seen[0].start.isoformat()=='2020-01-01' and seen[0].end.isoformat()=='2020-01-03'
    assert job.checkpoint['page_size']==100
    assert job.status=='succeeded' and job.checkpoint['history_coverage_qualified'] is False
    assert db_session.scalar(select(func.count()).select_from(PriceObservation))==1
    assert db_session.scalar(select(func.count()).select_from(VWAPDailyStat))==0


def test_rejected_first_page_continues_and_total_rejection_cannot_be_lost(db_session,monkeypatch,tmp_path):
    seen=[]
    class Client:
        def daily_prices(self,request):
            seen.append(request)
            return response([bar('0')],'page2') if len(seen)==1 else response([bar()])
    _,job=harness(db_session,monkeypatch,Client())
    run_collection(None,tmp_path,market='us',domain='prices')
    db_session.refresh(job)
    assert len(seen)==2 and job.status=='unsupported'
    assert job.checkpoint['rejected_total']==1


def test_global_entitlement_or_auth_error_stops_source_without_draining_pending_queue(db_session,monkeypatch,tmp_path):
    class Client:
        def daily_prices(self,request):
            raise ProviderError('alpaca','403')
    _,job=harness(db_session,monkeypatch,Client())
    another=plan_job(db_session,source='alpaca',domain='prices',entity_id='another',request={'market':'US'})
    job.priority=1;db_session.flush()
    result=run_collection(None,tmp_path,market='us',domain='prices',budget_seconds=1)
    db_session.refresh(another)
    assert result['processed_this_run']==1 and result['blocked_sources']['alpaca']=='403'
    assert another.status=='pending' and another.attempts==0


def test_repair_restarts_alpaca_traversal_and_preserves_previous_rejections(db_session):
    from backtest_data.ingestion.jobs import request_repair
    job=plan_job(db_session,source='alpaca',domain='prices',entity_id='repair',request={'market':'US'})
    job.status='unsupported'
    job.checkpoint={'next_page_token':'page2','request_anchor':utcnow().isoformat(),
                    'rejected_total':4,'source_rows_total':10000}
    db_session.flush()
    assert request_repair(db_session,domains=['prices'],market='us')==1
    assert 'next_page_token' not in job.checkpoint and 'request_anchor' not in job.checkpoint
    assert job.checkpoint['rejected_total']==0 and job.checkpoint['prior_traversal']['rejected_total']==4


def test_failed_second_page_repair_recollects_rejected_first_page(db_session,monkeypatch,tmp_path):
    from backtest_data.ingestion.jobs import request_repair
    seen=[]
    class BrokenClient:
        def daily_prices(self,request):
            seen.append(request.page_token)
            if request.page_token:
                raise ProviderError('alpaca','403')
            return response([bar('0')],'page2')
    module,job=harness(db_session,monkeypatch,BrokenClient())
    # This checks repair lineage, not the machine's ability to commit two SQL
    # pages inside five seconds while production backfills compete for I/O.
    monkeypatch.setattr(module.time,'monotonic',lambda:0.0)
    run_collection(None,tmp_path,market='us',domain='prices',budget_seconds=5)
    db_session.refresh(job)
    assert seen==[None,'page2'] and job.status=='unsupported'
    assert job.checkpoint['rejected_total']==1
    request_repair(db_session,domains=['prices'],market='us')
    class FixedClient:
        def daily_prices(self,request):
            seen.append(request.page_token)
            return response([bar()])
    monkeypatch.setattr(module,'AlpacaData',lambda *args:FixedClient())
    run_collection(None,tmp_path,market='us',domain='prices',budget_seconds=5)
    db_session.refresh(job)
    assert seen[-1] is None and job.status=='succeeded'
    assert job.checkpoint['prior_traversal']['rejected_total']==1


def test_explicit_alpaca_repair_does_not_claim_or_repair_kis_jobs(db_session,monkeypatch,tmp_path):
    class Client:
        def daily_prices(self,request):
            return response([bar()])
    module,job=harness(db_session,monkeypatch,Client())
    monkeypatch.setattr(module,'load_credentials',lambda **kwargs:SimpleNamespace(
        alpaca_key=True,alpaca_secret=True,kis_app_key=True,kis_app_secret=True))
    class ForbiddenKIS:
        def daily_prices(self,*args):
            raise AssertionError('explicit Alpaca collector cannot use KIS')
    monkeypatch.setattr(module,'KISQuotes',lambda *args:ForbiddenKIS())
    other=plan_job(db_session,source='kis_overseas',domain='prices',entity_id='unselected',
        request={'market':'US','symbol':'OTHER','output_start':'2020-01-01','output_end':'2020-01-03'})
    other.status='unsupported';other.checkpoint={'reason':'preserve_me'}
    db_session.flush()
    result=run_collection(None,tmp_path,market='us',domain='prices',mode='repair',
        budget_seconds=1,source_filter=['alpaca'])
    db_session.refresh(other)
    assert job.status=='succeeded' and result['requested_sources']==['alpaca']
    assert result['blocked_sources']=={}
    assert other.status=='unsupported' and other.attempts==0 and other.checkpoint=={'reason':'preserve_me'}
