from contextlib import contextmanager
from datetime import date
import json
from types import SimpleNamespace

from sqlalchemy import func, select

from backtest_data.catalog.models import Security
from backtest_data.db import utcnow
from backtest_data.ingestion.http import ProviderError, ProviderResponse
from backtest_data.ingestion.jobs import plan_job, request_repair
from backtest_data.ingestion.supervisor import _source_access, run_collection
from backtest_data.prices.models import PriceObservation, VWAPDailyStat


def response(day):
    rows = [] if day is None else [{'xymd': day, 'open': '100.0000', 'high': '101.0000',
        'low': '99.0000', 'clos': '100.0000', 'tvol': '10', 'tamt': '1000'}]
    payload = {'rt_cd': '0', 'output1': {'rsym': 'DNASAAPL', 'zdiv': '4'}, 'output2': rows}
    return ProviderResponse(json.dumps(payload).encode(), 200, utcnow(), 'application/json'), payload


def harness(session, monkeypatch, client):
    import backtest_data.ingestion.supervisor as module
    @contextmanager
    def tx(engine):
        yield session
    monkeypatch.setattr(module, 'transaction', tx)
    monkeypatch.setattr(module, 'ProviderHTTP', lambda engine: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(module, 'load_credentials', lambda **kwargs: SimpleNamespace(kis_app_key=True, kis_app_secret=True))
    monkeypatch.setattr(module, 'KISQuotes', lambda *args: client)
    security = Security(market='US', source_identifier='nasdaq_directory:AAPL',
        security_type='common_stock', name='Apple')
    session.add(security); session.flush()
    proof = {'market':'US', 'security_id':security.id, 'provider_symbol':'AAPL', 'exchange':'NAS',
        'qualified_current_mapping':True, 'source':'kis_us_master', 'realtime_symbol':'DNASAAPL',
        'master_hash':'a' * 64}
    job = plan_job(session, source='kis_overseas', domain='prices', entity_id=security.id,
        request={'market':'US', 'symbol':'AAPL', 'provider_symbol':'AAPL', 'exchange':'NAS',
            'mapping_evidence':proof, 'output_start':'2020-01-01', 'output_end':'2020-01-06'})
    return module, job


def test_us_kis_is_available_with_existing_keys_without_alpaca():
    allowed, blocked = _source_access(SimpleNamespace(kis_app_key=True, kis_app_secret=True), 'us', 'prices')
    assert 'kis_overseas' in allowed
    assert 'alpaca' in blocked


def test_short_us_pages_continue_to_explicit_start_boundary(db_session, monkeypatch, tmp_path):
    seen = []
    class Client:
        def overseas_daily_prices(self, request):
            seen.append(request)
            return response(['20200103', '20200102', '20191231'][len(seen)-1])
    _, job = harness(db_session, monkeypatch, Client())
    result = run_collection(None, tmp_path, market='us', domain='prices')
    db_session.refresh(job)
    assert [r.end for r in seen] == [date(2020,1,6), date(2020,1,2), date(2020,1,1)]
    assert all(r.captured_at == seen[0].captured_at for r in seen)
    assert job.status == 'succeeded' and job.checkpoint['boundary_reached'] is True
    assert job.checkpoint['history_coverage_qualified'] is False
    assert result['processed_this_run'] == 3
    assert db_session.scalar(select(func.count()).select_from(PriceObservation)) == 2
    assert db_session.scalar(select(func.count()).select_from(VWAPDailyStat)) == 0


def test_us_retry_preserves_backward_cursor_and_shared_source_block(db_session, monkeypatch, tmp_path):
    seen = []
    class Client:
        def overseas_daily_prices(self, request):
            seen.append(request.end)
            raise ProviderError('kis', 'temporary', retryable=True)
    _, job = harness(db_session, monkeypatch, Client())
    job.status = 'retry'; job.checkpoint = {'cursor_end':'2020-01-02', 'rejected_total':3}
    db_session.flush()
    result = run_collection(None, tmp_path, market='us', domain='prices')
    db_session.refresh(job)
    assert seen == [date(2020,1,2)] and job.status == 'retry'
    assert job.checkpoint['cursor_end'] == '2020-01-02' and job.checkpoint['rejected_total'] == 3
    assert result['blocked_sources']['kis_overseas'] == 'temporary'


def test_unmapped_us_repair_stays_unsupported(db_session):
    job = plan_job(db_session, source='kis_overseas', domain='prices', entity_id='unmapped',
        request={'market':'US', 'symbol':'UNKNOWN', 'mapping_status':'unsupported',
                 'mapping_reason':'exact_master_mapping_unavailable'})
    job.status = 'unsupported'; db_session.flush()
    assert request_repair(db_session, domains=['prices'], market='us') == 0
    assert job.status == 'unsupported'


def test_legacy_pending_unmapped_us_job_fails_closed_before_api(db_session, monkeypatch, tmp_path):
    class Client:
        def overseas_daily_prices(self, request):
            raise AssertionError('unmapped security must never call provider')
    _, job = harness(db_session, monkeypatch, Client())
    job.request = {'market':'US', 'symbol':'UNKNOWN', 'mapping_status':'unsupported',
                   'mapping_reason':'exact_master_mapping_unavailable'}
    db_session.flush()
    result = run_collection(None, tmp_path, market='us', domain='prices')
    db_session.refresh(job)
    assert job.status == 'unsupported' and job.lease_owner is None
    assert job.checkpoint['reason'] == 'exact_master_mapping_unavailable'
    assert result['processed_this_run'] == 1
