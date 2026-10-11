"""Loopback-only research API. Trusted SQL projections, registered strategies only."""
from datetime import date,datetime,timezone
from decimal import Decimal,DecimalException
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit
from typing import Annotated
from fastapi import FastAPI,HTTPException,Query,Request
from fastapi.responses import JSONResponse,Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,ConfigDict,Field,field_validator
from sqlalchemy.exc import SQLAlchemyError
from .dataset_store import DatasetStore,wire
from .engines import run_backtest,EngineUnavailable,UnsupportedResearchInput
from .features import compute_price_features
from .results import ImmutableResultStore,replay_result
from .screening import screen_subset
from .evaluation.reporting import build_analysis,export_csv

HEX='^[0-9a-f]{64}$'


class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid')
class Selection(Strict):
    security_id:str=Field(min_length=1,max_length=36)
    source_contract_id:str=Field(min_length=1,max_length=36)
    basis:str=Field(pattern='^(as_traded|provider_adjusted)$')
    feed:str=Field(max_length=80)
    venue:str=Field(max_length=80)
    trade_scope:str=Field(max_length=120)
    currency:str=Field(min_length=1,max_length=10)
    quantity_unit:str=Field(min_length=1,max_length=40)
    series_key:str=Field(pattern=HEX)
class DatasetRequest(Strict):
    selections:list[Selection]=Field(min_length=1,max_length=20)
    start:date
    end:date
    cutoff:datetime
class BacktestRequest(Strict):
    dataset_id:str=Field(pattern=HEX)
    raw_series_keys:list[Annotated[str,Field(pattern=HEX)]]=Field(min_length=1,max_length=20)
    feature_series_keys:list[Annotated[str,Field(pattern=HEX)]]|None=Field(default=None,max_length=20)
    strategy:str=Field(default='trend_sma',pattern='^(trend_sma|mean_reversion_rsi2|price_momentum)$')
    parameters:dict=Field(default_factory=dict)
    initial_cash:Decimal=Field(default=Decimal('100000'),gt=0,le=Decimal('1000000000000'))
    fee_bps:Decimal=Field(default=Decimal('0'),ge=0,le=1000)
    slippage_bps:Decimal=Field(default=Decimal('0'),ge=0,le=1000)
    @field_validator('parameters')
    @classmethod
    def bounded_parameters(cls,value):
        allowed={'fast_period','slow_period','rsi_period','entry_rsi','exit_rsi','max_hold_sessions',
            'allocation','top_n','momentum_lookback','momentum_skip','rebalance_every','positive_only'}
        if set(value)-allowed:raise ValueError('unregistered strategy parameter')
        for key,item in value.items():
            if not isinstance(item,(str,int,float,bool)) or len(str(item))>64:raise ValueError('bounded scalar parameter required')
            if key.endswith('period') or key in {'max_hold_sessions','top_n','momentum_lookback','momentum_skip','rebalance_every'}:
                if type(item) is not int or not (0 if key=='momentum_skip' else 1)<=item<=6000:raise ValueError('bounded integer period required')
            if key in {'allocation','entry_rsi','exit_rsi'}:
                try:number=Decimal(str(item))
                except DecimalException:raise ValueError('finite numerical strategy parameter required') from None
                if not number.is_finite() or not 0<=number<=(1 if key=='allocation' else 100):
                    raise ValueError('strategy parameter outside range')
            if key=='positive_only' and type(item) is not bool:raise ValueError('positive_only must be boolean')
        return value
class ScreenRequest(Strict):
    dataset_id:str=Field(pattern=HEX)
    basis:str=Field(default='as_traded',pattern='^(as_traded|provider_adjusted)$')
    feature:str=Field(default='momentum',pattern='^(momentum|sma|rsi|volatility|simple_return)$')
    top_n:int=Field(default=20,ge=1,le=100)


def create_app(*,projector=None,directory=None,frontend=None):
    if projector is None:
        from backtest_data.config import Settings
        from backtest_data.db import make_engine
        from .projection import SQLProjector
        settings=Settings.from_env();projector=SQLProjector(make_engine(settings.database_url),settings.raw_root)
    directory=Path(directory or 'artifacts/research-platform')
    datasets=DatasetStore(directory/'datasets');results=ImmutableResultStore(directory/'results')
    gate=threading.Lock()
    app=FastAPI(title='Backtest Research',docs_url='/api/docs',openapi_url='/api/openapi.json')
    app.state.datasets=datasets;app.state.results=results
    @app.middleware('http')
    async def local_access(request:Request,call_next):
        host=request.headers.get('host','').split(':')[0].lower()
        origin=request.headers.get('origin')
        if host not in {'127.0.0.1','localhost','testserver'} or (origin and
            (urlsplit(origin).netloc!=request.headers.get('host') or urlsplit(origin).scheme!=request.url.scheme)):
            return JSONResponse({'error':'local_same_origin_required'},status_code=403)
        length=request.headers.get('content-length','0')
        if not length.isdigit() or int(length)>64*1024:return JSONResponse({'error':'request_size_bound'},status_code=413)
        if request.method=='POST':
            chunks=[];size=0
            async for chunk in request.stream():
                size+=len(chunk)
                if size>64*1024:return JSONResponse({'error':'request_size_bound'},status_code=413)
                chunks.append(chunk)
            request._body=b''.join(chunks)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        return response
    @app.exception_handler(SQLAlchemyError)
    async def sql_error(request,exc):return JSONResponse({'error':'bounded_sql_unavailable','details_redacted':True},status_code=503)
    @app.exception_handler(ValueError)
    async def input_error(request,exc):return JSONResponse({'error':'input_or_evidence_unavailable','details_redacted':True},status_code=422)
    @app.exception_handler(FileNotFoundError)
    async def absent(request,exc):return JSONResponse({'error':'saved_input_or_result_not_found'},status_code=404)
    @app.exception_handler(EngineUnavailable)
    async def engine_error(request,exc):return JSONResponse({'error':'qualified_engine_unavailable'},status_code=503)
    def load(identifier):return datasets.get(identifier)
    @app.get('/api/health')
    def health():return {'status':'ready','engine':'backtrader','original_target_count':17758,'remote_collection_enabled':False}
    @app.get('/api/securities')
    def search(query:str=Query(default='',max_length=80),market:str=Query(default='both',pattern='^(US|KR|both)$'),
               limit:int=Query(default=20,ge=1,le=50)):
        return projector.search(query,market,limit)
    @app.get('/api/series')
    def series(security_id:str=Query(max_length=36),start:date=date(2020,1,1),end:date=date(2026,10,7)):
        return projector.series(security_id,start,end)
    @app.post('/api/datasets')
    def snapshot(body:DatasetRequest):
        data=projector.dataset([s.model_dump() for s in body.selections],body.start,body.end,body.cutoff)
        datasets.put(data)
        return wire(data)
    @app.get('/api/datasets/{identifier}')
    def dataset(identifier:str):return wire(load(identifier))
    @app.get('/api/features')
    def features(dataset_id:str=Query(pattern=HEX),series_key:str=Query(pattern=HEX),
                 sma_period:int=Query(default=20,ge=1,le=1000),rsi_period:int=Query(default=2,ge=1,le=1000)):
        return compute_price_features(load(dataset_id),series_key,sma_period=sma_period,rsi_period=rsi_period)
    @app.get('/api/publications')
    def publications(security_id:str=Query(max_length=36),cutoff:datetime=datetime(2026,10,8,tzinfo=timezone.utc)):
        return projector.publications(security_id,cutoff)
    @app.post('/api/backtests')
    def backtest(body:BacktestRequest):
        if not gate.acquire(blocking=False):raise HTTPException(409,'research_run_in_progress')
        try:
            result=run_backtest(load(body.dataset_id),raw_series_keys=body.raw_series_keys,
                feature_series_keys=body.feature_series_keys,strategy=body.strategy,parameters=body.parameters,
                initial_cash=str(body.initial_cash),fee_bps=str(body.fee_bps),slippage_bps=str(body.slippage_bps),engine='backtrader')
            results.put(result);return result
        finally:gate.release()
    @app.post('/api/screen')
    def screen(body:ScreenRequest):return screen_subset(load(body.dataset_id),basis=body.basis,feature=body.feature,top_n=body.top_n)
    @app.get('/api/results/{identifier}')
    def result(identifier:str):return results.get(identifier)
    @app.get('/api/results/{identifier}/analysis')
    def analysis(identifier:str,benchmark_series_key:str|None=Query(default=None,pattern=HEX)):
        saved=results.get(identifier)
        return build_analysis(saved,load(saved['dataset_id']),benchmark_series_key=benchmark_series_key)
    @app.get('/api/results/{identifier}/export')
    def export(identifier:str,format:str=Query(default='result',pattern='^(result|dataset|analysis|decisions|ledger|closed_portions)$'),
               benchmark_series_key:str|None=Query(default=None,pattern=HEX)):
        saved=results.get(identifier)
        # Result-store validation bounds the filename to an immutable 64-hex ID.
        if format in {'result','dataset','analysis'}:
            content=saved if format=='result' else wire(load(saved['dataset_id'])) if format=='dataset' else build_analysis(saved,load(saved['dataset_id']),benchmark_series_key=benchmark_series_key)
            body=json.dumps(content,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
            extension,media='json','application/json'
        else:
            if format=='decisions':
                rows=saved['selection_trace']
                fields=['result_id','dataset_id','session','security_id','raw_series_key','feature_series_key',
                    'eligible','exclusion_reason','rank','score','candidate_selected','selected','target_weight',
                    'rebalance_due','feature_values','feature_statuses','decision_reasons','pending_target',
                    'actual_fills','execution_events','actual_holding_quantity','actual_holding_weight',
                    'actual_held_sessions','feature_input_windows','input_observation_refs']
            elif format=='ledger':
                rows=saved['ledger']
                fields=['result_id','dataset_id','sequence','session','signal_session','security_id','raw_series_key',
                    'quantity','raw_open','price','fee','slippage_cost','tax']
            else:
                rows=build_analysis(saved,load(saved['dataset_id']))['trades']['closed_portions']
                fields=['result_id','dataset_id','security_id','raw_series_key','quantity','entry_session','exit_session',
                    'entry_sequence','exit_sequence','entry_ledger_index','exit_ledger_index','entry_price','exit_price',
                    'entry_fee','exit_fee','entry_tax','exit_tax','gross_pnl','net_pnl','net_return','holding_calendar_days']
            body=export_csv((dict(row,result_id=saved['result_id'],dataset_id=saved['dataset_id']) for row in rows),fields)
            extension,media='csv','text/csv'
        return Response(body,media_type=media,headers={'Content-Disposition':f'attachment; filename="{identifier}-{format}.{extension}"',
            'Cache-Control':'no-store'})
    @app.post('/api/results/{identifier}/replay')
    def replay(identifier:str):
        if not gate.acquire(blocking=False):raise HTTPException(409,'research_run_in_progress')
        try:
            previous=results.get(identifier);return replay_result(previous,load(previous['dataset_id']))
        finally:gate.release()
    frontend=Path(frontend or 'frontend/dist')
    if frontend.is_dir():app.mount('/',StaticFiles(directory=frontend,html=True),name='research-ui')
    return app


def main():
    import uvicorn
    uvicorn.run(create_app(),host='127.0.0.1',port=8765,log_level='warning',access_log=False)


if __name__=='__main__':main()
