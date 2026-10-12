"""Hosted batch boundaries retain completed and unclaimed PostgreSQL jobs."""
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
import json
from types import SimpleNamespace as N

import pytest
from sqlalchemy import select

from backtest_data.db import new_id, utcnow
from backtest_data.ingestion.http import ProviderResponse
from backtest_data.ingestion.models import CollectionJob, RawObject, TargetUniverseManifest
from backtest_data.ingestion.raw_store import RawStore
from backtest_data.prices.krx_quote_models import KRXQuoteObservation

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('limits', [{'max_requests': 1}, {'max_new_rows': 1}, {'max_raw_bytes': 1}])
def test_volume_boundary_commits_whole_response_and_next_batch_resumes(db_session, tmp_path, monkeypatch, limits):
    from backtest_data.prices import krx_quote_collection as module
    from backtest_data import providers
    target = TargetUniverseManifest(content_hash=new_id().replace('-', '') * 2,
        scope={'markets': ['KR'], 'output_start': '2016-01-01', 'output_end': '2026-10-07'},
        members=[{'market': 'KR', 'security_id': 'original'}], gaps=[])
    db_session.add(target); db_session.flush()
    module.plan_krx_quotes(db_session, target, date(2026, 10, 6), date(2026, 10, 7), services=['kospi'])
    jobs = list(db_session.scalars(select(CollectionJob).order_by(CollectionJob.entity_id)))
    jobs[1].checkpoint = {'request_anchor': 'preserved-anchor'}
    db_session.flush()
    seen = []; responses = []
    @contextmanager
    def transaction(_):
        with db_session.begin_nested():yield db_session
    def quotes(service, day):
        seen.append(day)
        payload = {'OutBlock_1': [{'BAS_DD': day.strftime('%Y%m%d'), 'ISU_CD': '095570',
            'ISU_NM': 'Fixture', 'TDD_OPNPRC': '2.5000', 'TDD_HGPRC': '3',
            'TDD_LWPRC': '2', 'TDD_CLSPRC': '2.50', 'ACC_TRDVOL': '5'}]}
        response = ProviderResponse(json.dumps(payload).encode(), 200, utcnow(), 'application/json')
        responses.append(response); return response, payload
    monkeypatch.setattr(module, 'transaction', transaction)
    monkeypatch.setattr(module, 'load_credentials', lambda **kwargs: N(krx_api_key=True))
    monkeypatch.setattr(module, 'ProviderHTTP', lambda _: N(close=lambda: None))
    monkeypatch.setattr(providers, 'KRXData', lambda *args: N(daily_quotes=quotes))
    first = module.run_krx_quote_collection(None, tmp_path, budget_seconds=6000, services=['kospi'], **limits)
    db_session.refresh(jobs[0]); db_session.refresh(jobs[1])
    assert first['batch_limit_reached'] and first['processed_this_run'] == 1
    assert jobs[0].status == 'succeeded' and jobs[1].status == 'pending' and jobs[1].attempts == 0
    assert jobs[1].checkpoint == {'request_anchor': 'preserved-anchor'}
    row = db_session.scalar(select(KRXQuoteObservation))
    assert row.open == Decimal('2.5000') and row.captured_at == responses[0].captured_at
    raw = db_session.get(RawObject, row.raw_id)
    assert RawStore(tmp_path).read(raw.storage_key) == responses[0].content
    second = module.run_krx_quote_collection(None, tmp_path, budget_seconds=6000, services=['kospi'], **limits)
    db_session.refresh(jobs[1])
    assert second['processed_this_run'] == 1 and jobs[1].status == 'succeeded'
    assert seen == [date(2026, 10, 6), date(2026, 10, 7)]
