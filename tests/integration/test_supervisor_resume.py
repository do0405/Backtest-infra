from contextlib import contextmanager
from datetime import date, timedelta
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import func, update

from backtest_data.catalog.models import Security
from backtest_data.db import utcnow
from backtest_data.ingestion.http import ProviderError, ProviderResponse
from backtest_data.ingestion.jobs import plan_job
from backtest_data.ingestion.supervisor import run_collection


def harness(db_session, monkeypatch, client):
    import backtest_data.ingestion.supervisor as module
    @contextmanager
    def tx(engine):
        yield db_session
    monkeypatch.setattr(module, "transaction", tx)
    monkeypatch.setattr(module, "ProviderHTTP", lambda engine: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(module, "load_credentials", lambda **kwargs: SimpleNamespace(kis_app_key=True, kis_app_secret=True))
    monkeypatch.setattr(module, "KISQuotes", lambda *args: client)
    monkeypatch.setattr(module, "OpenDART", lambda *args: None)
    monkeypatch.setattr(module, "SEC", lambda *args: None)
    security = Security(market="KR", source_identifier="supervisor-security", name="Test", security_type="unclassified")
    db_session.add(security)
    db_session.flush()
    return plan_job(db_session, source="kis", domain="prices", entity_id=security.id,
                    request={"market": "KR", "symbol": "000001", "output_start": "2016-01-01", "output_end": "2026-10-07"})


def test_retry_preserves_paging_checkpoint(db_session, monkeypatch, tmp_path):
    seen = []
    class Client:
        def daily_prices(self, request):
            seen.append(request.end)
            raise ProviderError("kis", "temporary", retryable=True)
    job = harness(db_session, monkeypatch, Client())
    job.status, job.checkpoint = "retry", {"cursor_end": "2020-01-30", "rejected_total": 7}
    db_session.flush()
    result = run_collection(None, tmp_path, market="kr", domain="prices")
    db_session.refresh(job)
    assert seen == [date(2020, 1, 30)]
    assert job.checkpoint["cursor_end"] == "2020-01-30"
    assert job.checkpoint["rejected_total"] == 7 and result["processed_this_run"] == 1


def test_all_invalid_full_page_still_advances_to_older_history(db_session, monkeypatch, tmp_path):
    seen = []
    class Client:
        def daily_prices(self, request):
            seen.append(request.end)
            rows = [{"stck_bsop_date": (date(2026, 10, 7) - timedelta(days=i)).strftime("%Y%m%d"),
                     "stck_oprc": "0", "stck_hgpr": "0", "stck_lwpr": "0", "stck_clpr": "0", "acml_vol": "0"} for i in range(100)] if len(seen) == 1 else []
            payload = {"rt_cd": "0", "output2": rows}
            return ProviderResponse(json.dumps(payload).encode(), 200, utcnow(), "application/json"), payload
    job = harness(db_session, monkeypatch, Client())
    result = run_collection(None, tmp_path, market="kr", domain="prices")
    assert len(seen) == 2 and seen[1] == date(2026, 6, 29)
    db_session.refresh(job)
    assert job.status == "unsupported" and job.checkpoint["rejected_total"] == 100
    assert result["processed_this_run"] == 2


def test_short_kr_page_continues_until_explicit_boundary(db_session, monkeypatch, tmp_path):
    seen = []
    class Client:
        def daily_prices(self, request):
            seen.append(request.end)
            day = ['20261007', '20160101'][len(seen)-1]
            payload = {'rt_cd':'0', 'output2':[{'stck_bsop_date':day, 'stck_oprc':'100',
                'stck_hgpr':'100', 'stck_lwpr':'100', 'stck_clpr':'100', 'acml_vol':'10', 'acml_tr_pbmn':'1000'}]}
            return ProviderResponse(json.dumps(payload).encode(), 200, utcnow(), 'application/json'), payload
    job = harness(db_session, monkeypatch, Client())
    result = run_collection(None, tmp_path, market='kr', domain='prices')
    db_session.refresh(job)
    assert seen == [date(2026,10,7), date(2026,10,6)]
    assert result['processed_this_run'] == 2 and job.status == 'succeeded'
    assert job.checkpoint['boundary_reached'] is True
    assert job.checkpoint['history_coverage_qualified'] is False


def test_dart_xbrl_maintenance_does_not_block_json_lane(db_session, monkeypatch, tmp_path):
    import backtest_data.ingestion.supervisor as module
    @contextmanager
    def tx(engine):
        yield db_session
    monkeypatch.setattr(module, 'transaction', tx)
    monkeypatch.setattr(module, 'ProviderHTTP', lambda engine: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(module, 'load_credentials', lambda **kwargs: SimpleNamespace(opendart_api_key=True))
    monkeypatch.setattr(module, 'plan_dart_json', lambda session: {'created_jobs':0})
    monkeypatch.setattr(module, 'collect_filing', lambda *args, **kwargs: (_ for _ in ()).throw(
        ProviderError('opendart', '800', retryable=True)))
    seen = []
    def collect(engine, request, **kwargs):
        seen.append(request); return {'status':'succeeded', 'coverage_complete':False}
    monkeypatch.setattr(module, 'collect_dart_json', collect)
    xbrl = plan_job(db_session, source='opendart', domain='filing_sql', entity_id='issuer', request={'market':'KR'})
    js = plan_job(db_session, source='opendart', domain='filing_json', entity_id='issuer', request={'market':'KR'})
    result = run_collection(None, tmp_path, market='kr', domain='fundamentals')
    db_session.refresh(xbrl); db_session.refresh(js)
    assert xbrl.status == 'retry' and js.status == 'succeeded' and len(seen) == 1
    assert 'opendart' not in result['blocked_sources']
    assert result['blocked_stages'] == [{'source':'opendart', 'domain':'filing_sql', 'reason':'800'}]


@pytest.mark.parametrize('failure',('fence','provider','validation'))
def test_expired_financial_lease_preserves_work_and_other_lane_progress(db_session,monkeypatch,tmp_path,failure):
    import backtest_data.ingestion.supervisor as module
    clock=[utcnow()]
    @contextmanager
    def tx(engine):
        yield db_session
    monkeypatch.setattr(module,'transaction',tx)
    monkeypatch.setattr(module,'utcnow',lambda:clock[0])
    monkeypatch.setattr(module,'ProviderHTTP',lambda engine:SimpleNamespace(close=lambda:None))
    monkeypatch.setattr(module,'load_credentials',lambda **kwargs:SimpleNamespace(opendart_api_key=True))
    monkeypatch.setattr(module,'plan_dart_json',lambda session:{'created_jobs':0})
    def delayed(engine,request,**kwargs):
        clock[0]+=timedelta(minutes=6)
        from backtest_data.ingestion.models import CollectionJob
        # Expiry is authoritative server state for the production alias.
        db_session.execute(update(CollectionJob).where(CollectionJob.source == 'opendart',
            CollectionJob.domain == 'filing_sql', CollectionJob.entity_id == 'slow-fixture',
            CollectionJob.status == 'running').values(lease_until=func.clock_timestamp()-timedelta(seconds=1)))
        if failure=='fence':kwargs['fence'](db_session)
        elif failure=='provider':raise ProviderError('opendart','temporary',retryable=True)
        else:raise ValueError('invalid fixture response')
    monkeypatch.setattr(module,'collect_filing',delayed)
    monkeypatch.setattr(module,'collect_dart_json',lambda *args,**kwargs:{'status':'succeeded','coverage_complete':False})
    original=plan_job(db_session,source='opendart',domain='filing_sql',entity_id='slow-fixture',request={'market':'KR'})
    original.checkpoint={'preserved':'before_attempt'}
    js=plan_job(db_session,source='opendart',domain='filing_json',entity_id='other-fixture',request={'market':'KR'})
    db_session.flush()
    result=run_collection(None,tmp_path,market='kr',domain='fundamentals')
    db_session.refresh(original);db_session.refresh(js)
    assert original.status=='running' and original.checkpoint=={'preserved':'before_attempt'}
    assert js.status=='succeeded' and result['processed_this_run']==2
    assert 'opendart' not in result['blocked_sources']
    assert result['blocked_stages']==[{'source':'opendart','domain':'filing_sql','reason':'lease_lost'}]
    assert any(item['status']=='lease_lost' for item in result['outcomes'])
