from datetime import date, datetime, timezone
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from backtest_research.contracts import AnalysisDataset, Bar, SeriesIdentity
from backtest_research.api import create_app
from backtest_research.dataset_store import DatasetStore


def fixture():
    clock = datetime(2026,10,8,tzinfo=timezone.utc)
    identity = SeriesIdentity('A','C','US','fixture','USD','XNYS','fixture','daily','as_traded',(),'as_traded','shares')
    bars = [Bar(str(i), identity, date(2026,10,5+i), Decimal(p),Decimal(p),Decimal(p),Decimal(p),
        Decimal(100), clock, datetime(2026,10,6+i,tzinfo=timezone.utc), 'a'*64) for i,p in enumerate(['10','12','11'])]
    return AnalysisDataset.create(bars=bars,sessions=[b.session for b in bars],cutoff=clock,manifest_id='M',
        manifest_hash='b'*64,original_target_count=17758,source_snapshot_at=clock,quality_gaps=(),origin='test_fixture')


class Projector:
    def search(self, query, market, limit):
        return [{'security_id':'A','name':'Fixture','market':'US','identifier':'A','historical_identity':'unknown'}]
    def series(self, security_id, start, end):
        return []
    def dataset(self, selections, start, end, cutoff):
        return fixture()
    def publications(self, security_id, cutoff):
        return []


def test_immutable_dataset_roundtrip_and_tamper(tmp_path):
    store = DatasetStore(tmp_path)
    data = fixture()
    store.put(data)
    assert store.get(data.dataset_id) == data
    path = tmp_path / (data.dataset_id+'.json')
    path.write_text(path.read_text().replace('"10"','"999"'))
    with pytest.raises(ValueError):
        store.get(data.dataset_id)


def test_api_is_local_only_and_blocks_cross_origin_mutations(tmp_path):
    client = TestClient(create_app(projector=Projector(),directory=tmp_path))
    assert client.get('/api/health').status_code == 200
    assert client.get('/api/health',headers={'Host':'evil.example'}).status_code == 403
    assert client.post('/api/datasets',headers={'Origin':'https://evil.example'},json={}).status_code == 403
    assert client.get('/api/securities?query=A&market=US&limit=10000').status_code == 422


def test_api_real_framework_result_has_decisions_and_replays(tmp_path):
    client = TestClient(create_app(projector=Projector(),directory=tmp_path))
    data = fixture()
    DatasetStore(tmp_path/'datasets').put(data)
    response = client.post('/api/backtests',json={'dataset_id':data.dataset_id,
        'raw_series_keys':[data.series[0].key],'strategy':'trend_sma',
        'parameters':{'fast_period':1,'slow_period':2},'initial_cash':'100'})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['origin'] == 'test_fixture'
    assert result['engine']['actual_framework_execution'] is True
    assert len(result['decisions']) == 3
    replay = client.post('/api/results/'+result['result_id']+'/replay')
    assert replay.status_code == 200
    assert replay.json()['result_id'] == result['result_id']


def test_api_rejects_code_parameters_and_path_traversal(tmp_path):
    client = TestClient(create_app(projector=Projector(),directory=tmp_path))
    assert client.post('/api/backtests',json={'dataset_id':'../x','raw_series_keys':[],'code':'print(1)'}).status_code == 422
    assert client.get('/api/datasets/'+'0'*64).status_code == 404


def test_api_caps_streamed_body_and_series_elements(tmp_path):
    client=TestClient(create_app(projector=Projector(),directory=tmp_path))
    response=client.post('/api/datasets',content=(b'x'*40000 for _ in range(2)),headers={'Content-Type':'application/json'})
    assert response.status_code==413
    response=client.post('/api/backtests',json={'dataset_id':'a'*64,'raw_series_keys':['b'*100000]})
    assert response.status_code in (413,422)
    response=client.post('/api/backtests',json={'dataset_id':'a'*64,'raw_series_keys':['bad-key']})
    assert response.status_code==422


def test_scheme_and_invalid_strategy_numbers_are_rejected(tmp_path):
    client=TestClient(create_app(projector=Projector(),directory=tmp_path))
    assert client.post('/api/backtests',headers={'Origin':'https://testserver'},json={}).status_code==403
    for value in ('not-a-number','NaN','Infinity'):
        response=client.post('/api/backtests',json={'dataset_id':'a'*64,'raw_series_keys':['b'*64],
            'parameters':{'allocation':value}})
        assert response.status_code==422


def test_read_only_derived_analysis_and_downloads_preserve_saved_result(tmp_path):
    import csv
    import io
    from backtest_research.engines import run_backtest
    from backtest_research.results import ImmutableResultStore
    client = TestClient(create_app(projector=Projector(), directory=tmp_path))
    data = fixture()
    DatasetStore(tmp_path/'datasets').put(data)
    original = run_backtest(data, raw_series_keys=[data.series[0].key], initial_cash='100',
                            parameters={'fast_period': 1, 'slow_period': 2})
    store = ImmutableResultStore(tmp_path/'results')
    store.put(original)
    report = client.get(f"/api/results/{original['result_id']}/analysis", params={'benchmark_series_key': data.series[0].key})
    assert report.status_code == 200
    assert report.json()['benchmark']['status'] == 'available'
    assert store.get(original['result_id']) == original
    for kind in ('result', 'dataset', 'analysis'):
        response = client.get(f"/api/results/{original['result_id']}/export", params={'format': kind})
        assert response.status_code == 200
        assert response.headers['content-disposition'].startswith('attachment; filename=')
        assert response.headers['x-content-type-options'] == 'nosniff'
        expected_key = 'result_id' if kind == 'result' else 'dataset_id' if kind == 'dataset' else 'analysis_id'
        assert expected_key in response.json()
    csv_result = client.get(f"/api/results/{original['result_id']}/export?format=decisions")
    rows = list(csv.DictReader(io.StringIO(csv_result.text)))
    assert len(rows) == len(original['selection_trace'])
    assert rows[0]['result_id'] == original['result_id']
    assert rows[0]['session'] == original['selection_trace'][0]['session']
    assert client.get(f"/api/results/{original['result_id']}/export?format=anything").status_code == 422
    assert client.get(f"/api/results/{original['result_id']}/analysis?benchmark_series_key=../x").status_code == 422
