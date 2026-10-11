from contextlib import contextmanager
from datetime import date, datetime, timezone
import importlib
from types import SimpleNamespace as N

import pytest

from backtest_data.remote.codec import PaginationCursor, dumps, loads

NOW = datetime(2026,10,10,5,tzinfo=timezone.utc)


def api():
    try:return importlib.import_module('backtest_data.remote.source_handlers')
    except ModuleNotFoundError:pytest.fail('portable owned-source handlers are required')


def manifest():
    return N(id='whole',content_hash='a'*64,scope={'markets':['KR','US'],'output_start':'2016-01-01','output_end':'2026-10-07'},
        members=[{'market':'KR','security_id':str(i)} for i in range(4507)]+
            [{'market':'US','security_id':str(i),'symbol':'AAPL'} for i in range(13251)])


def slot(source,handler=None):
    result={'source':source,'market':'kr' if source=='krx' else 'both','domain':'prices' if source in {'krx','alpaca'} else 'fundamentals',
        'owner':'remote','local_enabled':False,'account_id':'opaque-account'}
    if handler:result['handler']=handler
    return result


def supplier(scope,**changes):
    return N(**(dict(id='supplier',manifest_id=scope['manifest_id'],source='alpaca',adjustment='split',
        target_hash=scope['target_hash'],target_count=17758,market_target_count=13251,
        output_start=date(2016,1,1),output_end=date(2026,10,7),status='budget_deferred',last_reason=None,updated_at=NOW,
        cursor={'member_index':5,'pages':7,'inserted':600,'source_rows':700,'rejected':100,
            'page_token':'page-next','request_anchor':NOW.isoformat()}) | changes))


def progress():
    return {'manifest_id':'whole','target_count':17758,'output_start':'2016-01-01','output_end':'2026-10-07',
        'completed':{'index:2016:0':{'document_id':'original-doc','source_rows':50}},'catalogs':{},'errors':{},
        'actions':{'2016':{'pages':1,'next_page_token':'next-action-page'}},
        'trade_proof':{'pages':[{'document_id':'trade-doc','page_token':None}],'next_page_token':'next-trade-page'},
        'nport':{'2019q4_nport':{'section':'FUND_REPORTED_HOLDING','offset':1000,'document_id':'nport-doc'}}}


def test_whole_manifest_scope_rejects_reduction_or_backfill_replacement():
    target=manifest()
    scope=api().full_manifest_scope(target)
    assert scope['target_count']==17758 and scope['market_target_counts']=={'KR':4507,'US':13251}
    target.members=target.members[:10]
    with pytest.raises(ValueError,match='whole'):api().full_manifest_scope(target)
    target=manifest();target.scope['output_end']='2026-10-09'
    with pytest.raises(ValueError,match='original'):api().full_manifest_scope(target)


def test_krx_plan_preserves_all_ten_services_and_bounded_original_calendar_progress():
    mod=api();scope=mod.full_manifest_scope(manifest())
    first=mod.krx_plan_window(scope,as_of=NOW,max_calendar_days=31)
    assert first['start']==date(2016,1,1) and first['end']==date(2016,1,31)
    assert len(first['services'])==10 and first['full_request_target']==39330
    assert first['rollforward_status']=='explicit_completed_day_refresh'
    resumed=mod.krx_plan_window(scope,as_of=NOW,prior={'scope':scope,'planned_through':'2016-01-31'},max_calendar_days=31)
    assert resumed['start']==date(2016,2,1) and resumed['end']==date(2016,3,2)
    assert resumed['scope']==scope and resumed['coverage_complete'] is False


def test_krx_planner_uses_last_completed_korean_day_and_refuses_foreign_checkpoint():
    mod=api();scope=mod.full_manifest_scope(manifest())
    result=mod.krx_plan_window(scope,as_of=datetime(2016,1,1,15,tzinfo=timezone.utc),max_calendar_days=31)
    assert result['end']==date(2016,1,1)
    other={**scope,'target_hash':'b'*64}
    with pytest.raises(ValueError,match='binding'):mod.krx_plan_window(scope,as_of=NOW,prior={'scope':other,'planned_through':'2016-01-31'})


def test_supplemental_progress_roundtrips_typed_pages_and_exact_committed_offsets():
    mod=api();scope=mod.full_manifest_scope(manifest())
    wire=loads(dumps(mod.portable_supplemental_progress(progress(),scope)))
    assert isinstance(wire['progress']['actions']['2016']['next_page_token'],PaginationCursor)
    runtime=mod.runtime_supplemental_progress(wire,scope)
    assert runtime==progress()
    assert runtime['nport']['2019q4_nport']['offset']==1000


@pytest.mark.parametrize('bad', ['foreign_scope','secret','unrecognized_cursor'])
def test_supplemental_progress_never_transfers_credentials_or_unrecognized_tokens(bad):
    mod=api();scope=mod.full_manifest_scope(manifest());state=progress()
    if bad=='foreign_scope':state['target_count']=12
    elif bad=='secret':state['headers']={'Authorization':'hidden'}
    else:state['unknown']={'page_token':'opaque'}
    with pytest.raises(ValueError):mod.portable_supplemental_progress(state,scope)


def test_supplier_state_keeps_exact_member_page_and_request_anchor():
    mod=api();scope=mod.full_manifest_scope(manifest())
    wire=loads(dumps(mod.portable_supplier_state(supplier(scope),scope)))
    assert wire['cursor']['page_token'].value=='page-next'
    assert wire['cursor']['request_anchor']==NOW.isoformat()
    assert wire['scope']==scope
    class Empty:
        def scalar(self,_):return None
        def add(self,row):self.row=row
        def flush(self):pass
    session=Empty()
    restored=mod.restore_supplier_state(session,wire,scope,authority='new_remote_ephemeral_database')
    assert restored.cursor['member_index']==5 and restored.cursor['page_token']=='page-next'
    assert restored.cursor['request_anchor']==NOW.isoformat()
    assert restored.target_count==17758 and restored.market_target_count==13251


def test_supplier_restore_never_overwrites_local_or_existing_operational_state():
    mod=api();scope=mod.full_manifest_scope(manifest());wire=mod.portable_supplier_state(supplier(scope),scope)
    class Existing:
        def scalar(self,_):return 'existing-state'
        def add(self,_):pytest.fail('operational state was mutated')
    with pytest.raises(ValueError,match='authority'):mod.restore_supplier_state(Existing(),wire,scope,authority='local')
    with pytest.raises(ValueError,match='empty'):mod.restore_supplier_state(Existing(),wire,scope,authority='new_remote_ephemeral_database')


@pytest.mark.parametrize('change', ['untyped','private','member_overflow','anchor_missing','unknown_adjustment'])
def test_supplier_state_fails_closed_without_inventing_continuation(change):
    mod=api();scope=mod.full_manifest_scope(manifest());state=supplier(scope)
    if change=='private':state.cursor['lease_owner']='local'
    if change=='member_overflow':state.cursor['member_index']=13252
    if change=='anchor_missing':state.cursor['request_anchor']=None
    if change=='unknown_adjustment':state.adjustment='raw'
    if change=='untyped':
        wire=mod.portable_supplier_state(state,scope);wire['cursor']['page_token']='untyped'
        with pytest.raises(ValueError):mod.restore_supplier_state(N(),wire,scope,authority='new_remote_ephemeral_database')
    else:
        with pytest.raises(ValueError):mod.portable_supplier_state(state,scope)


def test_handlers_require_remote_ownership_before_any_sql_or_provider_calls():
    bad=slot('krx');bad['local_enabled']=True
    with pytest.raises(ValueError,match='local'):api().run_owned_handlers(N(),PathStub(),manifest_id='whole',slots=[bad],owned_sources={'krx'},
        checkpoint={},budget_seconds=10,work_root='artifacts/tests/no-source',authority='new_remote_ephemeral_database')


class PathStub:
    def __fspath__(self):return 'artifacts/tests/no-source/raw'


def test_supplemental_dispatch_requires_all_credential_providers_owned():
    with pytest.raises(ValueError,match='dependencies'):api().run_owned_handlers(N(),PathStub(),manifest_id='whole',slots=[slot('supplemental')],
        owned_sources={'supplemental'},checkpoint={},budget_seconds=10,work_root='artifacts/tests/no-source')


def test_dispatch_only_calls_explicit_handlers_with_bounded_budget_and_portable_state(monkeypatch):
    mod=api();target=manifest();calls=[]
    @contextmanager
    def txn(_):yield N(get=lambda *_:target)
    monkeypatch.setattr(mod,'transaction',txn)
    def krx(*args,**kwargs):calls.append(kwargs);return {'scope':kwargs['scope'],'planned_through':'2016-01-31'},{'handler':'krx_quotes','status':'partial'}
    monkeypatch.setattr(mod,'krx_quote_step',krx)
    result=mod.run_owned_handlers(N(),PathStub(),manifest_id='whole',slots=[slot('krx'),slot('alpaca')],owned_sources={'krx','alpaca'},
        checkpoint={},budget_seconds=10,work_root='artifacts/tests/no-source',as_of=NOW)
    assert len(calls)==1 and 0<calls[0]['budget_seconds']<=10
    assert result['source_handler_state']['krx_quotes']['planned_through']=='2016-01-31'
    assert result['source_handler_outcomes']==[{'handler':'krx_quotes','status':'partial'}]
    dumps(result)


def test_one_handler_failure_preserves_prior_and_allows_other_owned_source_to_export(monkeypatch):
    mod=api();target=manifest();scope=mod.full_manifest_scope(target)
    old={'scope':scope,'planned_through':'2016-01-31'}
    @contextmanager
    def txn(_):yield N(get=lambda *_:target)
    monkeypatch.setattr(mod,'transaction',txn)
    def failure(*args,**kwargs):raise RuntimeError('secret provider configuration must never enter checkpoint')
    def adjusted(*args,**kwargs):
        return mod.portable_supplier_state(supplier(scope),scope),{'handler':'supplier_adjusted','status':'budget_deferred'}
    monkeypatch.setattr(mod,'krx_quote_step',failure)
    monkeypatch.setattr(mod,'supplier_adjusted_step',adjusted)
    result=mod.run_owned_handlers(N(),PathStub(),manifest_id='whole',slots=[slot('krx'),slot('alpaca','supplier_adjusted')],
        owned_sources={'krx','alpaca'},checkpoint={'source_handler_state':{'krx_quotes':old}},budget_seconds=10,
        work_root='artifacts/tests/no-source',as_of=NOW)
    assert result['source_handler_state']['krx_quotes']==old
    assert result['source_handler_outcomes'][0]['status']=='source_error'
    assert result['source_handler_outcomes'][0]['reason']=='RuntimeError'
    assert result['source_handler_outcomes'][1]['status']=='budget_deferred'
    assert 'secret provider' not in dumps(result)


def test_krx_adapter_commits_planning_before_advancing_and_runs_all_services(monkeypatch):
    mod=api();target=manifest();scope=mod.full_manifest_scope(target);order=[]
    @contextmanager
    def txn(_):
        order.append('begin');yield N(get=lambda *_:target,scalars=lambda _:N(yield_per=lambda _:[]));order.append('commit')
    monkeypatch.setattr(mod,'transaction',txn)
    def plan(session,target,start,end,services,**kwargs):
        order.append('plan');assert len(services)==10;return {'created_jobs':310}
    def collect(*args,**kwargs):
        assert order[-1]=='commit';order.append('collect');assert len(kwargs['services'])==10
        return {'status':'partial','elapsed_seconds':0.25}
    monkeypatch.setattr(mod,'plan_krx_quotes',plan);monkeypatch.setattr(mod,'run_krx_quote_collection',collect)
    state,outcome=mod.krx_quote_step(N(),PathStub(),scope=scope,previous=None,budget_seconds=10,as_of=NOW)
    assert state['planned_through']=='2016-01-31'
    assert order==['begin','plan','commit','begin','plan','commit','begin','plan','commit','collect','begin','commit']
    dumps(outcome)


def test_supplemental_adapter_reads_durable_decimal_progress_and_closes_runtime(monkeypatch):
    from decimal import Decimal
    from backtest_data.supplemental import collector as library
    mod=api();scope=mod.full_manifest_scope(manifest());closed=[];paths={}
    class MemoryPath:
        def __init__(self,value):self.value=str(value)
        def __truediv__(self,value):return MemoryPath(self.value+'/'+value)
        @property
        def parent(self):return self
        def mkdir(self,**_):pass
        def exists(self):return self.value in paths
        def write_text(self,text,**_):paths[self.value]=text
        def read_text(self,**_):return paths[self.value]
    class Collector:
        def __init__(self,engine,settings,path,manifest_id,**kwargs):
            self.path=path;self.state=progress();self.state['catalogs']={'fund':[{'fee_rate':Decimal('0.0123')}]}
            if kwargs.get('requested_window'):
                window=kwargs['requested_window'];self.state.update(output_start=window['start'].isoformat(),output_end=window['end'].isoformat(),
                    original_output_start='2016-01-01',original_output_end='2026-10-07')
            self.http=N(close=lambda:closed.append('http'));self.nport_iterators={'q':N(close=lambda:closed.append('iterator'))}
        def run(self,budget):
            import json
            from backtest_data.supplemental.storage import exact_json
            assert 0<budget<=10;self.path.write_text(json.dumps(exact_json(self.state)))
            return {'status':'budget_deferred','target_count':17758,'progress_path':'never/export/local/path'}
    monkeypatch.setattr(mod,'Path',MemoryPath);monkeypatch.setattr(library,'SupplementalCollector',Collector)
    state,outcome=mod.supplemental_step(N(),'raw',scope=scope,previous=None,budget_seconds=10,work_root='work',as_of=NOW)
    assert state['backfill']['progress']['catalogs']['fund'][0]['fee_rate']=='0.0123'
    assert closed==['iterator','http','iterator','http']
    assert all('progress_path' not in item['collection'] for item in outcome['window_outcomes'])
    assert mod.runtime_supplemental_progress(state['backfill'],scope)['actions']['2016']['next_page_token']=='next-action-page'


def test_failed_supplier_stage_recovers_last_committed_page_cursor_without_auth(monkeypatch):
    mod=api();target=manifest();scope=mod.full_manifest_scope(target);current=supplier(scope)
    @contextmanager
    def txn(_):yield N(get=lambda *_:target,scalar=lambda _:current,scalars=lambda _:N(yield_per=lambda _:[]))
    monkeypatch.setattr(mod,'transaction',txn)
    def failure(*args,**kwargs):raise TimeoutError('Bearer secret must not be included')
    monkeypatch.setattr(mod,'supplier_adjusted_step',failure)
    result=mod.run_owned_handlers(N(),PathStub(),manifest_id='whole',slots=[slot('alpaca','supplier_adjusted')],owned_sources={'alpaca'},
        checkpoint={},budget_seconds=10,work_root='artifacts/tests/no-source',as_of=NOW)
    wire=result['source_handler_state']['supplier_adjusted']
    assert wire['backfill']['cursor']['member_index']==5 and wire['backfill']['cursor']['page_token'].value=='page-next'
    assert result['source_handler_outcomes'][0]['reason']=='TimeoutError'
    assert 'Bearer' not in dumps(result)


def planning_session():
    class Session:
        def __init__(self):self.requests=[];self.identities=set()
        def scalars(self,_):return N(yield_per=lambda _:iter(self.identities))
        def execute(self,statement):
            from sqlalchemy.dialects import postgresql
            params=statement.compile(dialect=postgresql.dialect()).params
            requests=[value for key,value in params.items() if key.startswith('request_m')]
            self.requests.extend(requests)
            self.identities.update(value for key,value in params.items() if key.startswith('identity_hash_m'))
            return N(all=lambda:[(str(index),) for index in range(len(requests))])
        def flush(self):pass
    return Session()


def persisted_manifest():
    from backtest_data.ingestion.models import TargetUniverseManifest
    target=manifest()
    return TargetUniverseManifest(id=target.id,content_hash=target.content_hash,scope=target.scope,members=target.members)


def test_native_krx_refresh_is_explicit_completed_only_and_keeps_original_manifest(monkeypatch):
    from backtest_data.prices import krx_quote_collection as library
    monkeypatch.setattr(library,'utcnow',lambda:NOW)
    session=planning_session();target=persisted_manifest()
    with pytest.raises(ValueError,match='outside'):library.plan_krx_quotes(session,target,date(2026,10,8),date(2026,10,9))
    assert session.requests==[]
    first=library.plan_krx_quotes(session,target,date(2026,10,8),date(2026,10,9),refresh=True,refresh_anchor=date(2026,10,10))
    assert first['planned_requests']==first['created_jobs']==20
    assert all(request['manifest_id']=='whole' and request['original_output_start']=='2016-01-01'
        and request['original_output_end']=='2026-10-07' and request['refresh_anchor']=='2026-10-10' for request in session.requests)
    again=library.plan_krx_quotes(session,target,date(2026,10,8),date(2026,10,9),refresh=True,refresh_anchor=date(2026,10,10))
    assert again['created_jobs']==0 and again['reused_jobs']==20
    with pytest.raises(ValueError,match='completed'):library.plan_krx_quotes(session,target,date(2026,10,10),date(2026,10,10),refresh=True)
    assert target.scope['output_end']=='2026-10-07' and len(target.members)==17758


def test_krx_rolling14_and_catchup_are_separate_from_full_original_backfill():
    mod=api();scope=mod.full_manifest_scope(manifest())
    window=mod.krx_plan_window(scope,as_of=NOW,prior={'scope':scope,'planned_through':'2016-01-31',
        'refresh_planned_through':'2026-09-01'},max_calendar_days=31,completed_through='2026-09-01')
    assert window['start']==date(2016,2,1) and window['end']==date(2016,3,2)
    assert window['refresh_windows']==[
        {'kind':'catchup','start':date(2026,9,2),'end':date(2026,10,2)},
        {'kind':'rolling','start':date(2026,9,26),'end':date(2026,10,9)}]
    assert window['refresh_anchor']==date(2026,10,10)
    assert window['scope']['output_end']=='2026-10-07'


class MemoryPath:
    def __init__(self,value,files=None):
        self.value=str(value);self.files=files if files is not None else getattr(value,'files',{})
    def __str__(self):return self.value
    def __fspath__(self):return self.value
    def __truediv__(self,value):return MemoryPath(self.value+'/'+value,self.files)
    @property
    def parent(self):return self
    def mkdir(self,**_):pass
    def exists(self):return self.value in self.files
    def write_text(self,text,**_):self.files[self.value]=text
    def read_text(self,**_):return self.files[self.value]


def native_supplemental(monkeypatch,*,window=None,free=50*1024**3,as_of=NOW,prior=None):
    from backtest_data.supplemental import collector as library
    target=manifest();files={}
    path=MemoryPath('progress',files)
    if prior is not None:
        import json
        files['progress']=json.dumps(prior)
    @contextmanager
    def txn(_):yield N(get=lambda *_:target)
    monkeypatch.setattr(library,'transaction',txn);monkeypatch.setattr(library,'Path',MemoryPath)
    monkeypatch.setattr(library,'utcnow',lambda:as_of)
    monkeypatch.setattr(library,'SourceHTTP',lambda _:N(close=lambda:None))
    monkeypatch.setattr(library,'load_credentials',lambda **_:N(sec_user_agent='public@example.test',
        alpha_vantage_api_key=None,alpaca_key=None,alpaca_secret=None))
    return library.SupplementalCollector(N(),N(raw_root='raw'),path,manifest_id='whole',
        requested_window=window,min_free_bytes=free)


def test_native_supplemental_refresh_retains_original_scope_and_uses_last_complete_shared_day(monkeypatch):
    window={'start':date(2026,9,26),'end':date(2026,10,9)}
    collector=native_supplemental(monkeypatch,window=window,free=2*1024**3)
    assert collector.start==window['start'] and collector.end==window['end']
    assert collector.original_start==date(2016,1,1) and collector.original_end==date(2026,10,7)
    assert collector.scope['output_end']=='2026-10-07' and collector.target_count==17758
    assert collector.state['original_output_end']=='2026-10-07'
    assert collector.nport_quarters==[(2026,3),(2026,2)]
    with pytest.raises(ValueError,match='completed'):
        native_supplemental(monkeypatch,window=window,as_of=datetime(2026,10,10,tzinfo=timezone.utc))


def test_native_supplemental_defaults_and_disk_floor_do_not_change_local_backfill(monkeypatch):
    collector=native_supplemental(monkeypatch)
    assert (collector.start,collector.end)==(date(2016,1,1),date(2026,10,7))
    assert collector.min_free_bytes==50*1024**3
    assert collector.nport_quarters[0]==(2019,4) and collector.nport_quarters[-1]==(2026,3)
    with pytest.raises(ValueError,match='disk'):native_supplemental(monkeypatch,free=1024)


def test_native_supplemental_never_relabels_old_progress_with_new_requested_window(monkeypatch):
    with pytest.raises(ValueError,match='window'):
        native_supplemental(monkeypatch,window={'start':date(2026,10,8),'end':date(2026,10,9)},prior=progress())


def test_windowed_supplemental_progress_binds_requested_dates_separately_from_original_manifest():
    mod=api();scope=mod.full_manifest_scope(manifest());raw=progress()
    window={'start':date(2026,9,26),'end':date(2026,10,9)}
    raw.update(output_start='2026-09-26',output_end='2026-10-09',original_output_start='2016-01-01',original_output_end='2026-10-07')
    wire=mod.portable_supplemental_progress(raw,scope,requested_window=window)
    assert wire['scope']['output_end']=='2026-10-07' and wire['requested_window']==window
    assert mod.runtime_supplemental_progress(loads(dumps(wire)),scope)==raw


def test_supplier_refresh_checkpoint_restores_into_new_window_table_without_old_state_mutation():
    from backtest_data.prices.supplier_refresh_models import SupplierPriceRefreshState
    mod=api();scope=mod.full_manifest_scope(manifest());window={'start':date(2026,9,26),'end':date(2026,10,9)}
    state=supplier(scope,output_start=window['start'],output_end=window['end'])
    wire=mod.portable_supplier_state(state,scope,requested_window=window)
    class Empty:
        def scalar(self,query):self.query=query;return None
        def add(self,row):self.row=row
        def flush(self):pass
    session=Empty()
    restored=mod.restore_supplier_state(session,loads(dumps(wire)),scope,authority='new_remote_ephemeral_database')
    assert isinstance(restored,SupplierPriceRefreshState)
    assert restored.output_end==date(2026,10,9) and restored.target_count==17758
    assert 'supplier_price_refresh_state' in str(session.query)


def test_refresh_schedule_runs_latest_and_oldest_unfinished_window_alongside_backfill():
    mod=api();scope=mod.full_manifest_scope(manifest())
    older={'start':date(2026,9,25),'end':date(2026,10,8)}
    old={'scope':scope,'requested_window':older,'status':'budget_deferred'}
    state={'scope':scope,'version':2,'backfill':None,'refresh_windows':{'2026-09-25_2026-10-08':old}}
    work=mod.refresh_work_windows(state,as_of=NOW)
    assert work[0][1]=={'start':date(2026,9,26),'end':date(2026,10,9)}
    assert work[1]==('refresh',older,old)
    assert work[2]==('backfill',None,None)


def test_shared_refresh_date_rejects_every_future_checkpoint_even_when_not_oldest():
    mod=api();scope=mod.full_manifest_scope(manifest())
    old={'scope':scope,'requested_window':{'start':date(2026,9,25),'end':date(2026,10,8)},'status':'budget_deferred'}
    future={'scope':scope,'requested_window':{'start':date(2026,9,28),'end':date(2026,10,11)},'status':'request_queue_complete'}
    state={'scope':scope,'version':2,'backfill':None,'refresh_windows':{'old':old,'future':future}}
    with pytest.raises(ValueError,match='future'):mod.refresh_work_windows(state,as_of=NOW)


def test_native_supplemental_disk_override_is_explicit_and_local_default_stays_blocked(monkeypatch):
    from backtest_data.supplemental import collector as library
    local=native_supplemental(monkeypatch)
    remote=native_supplemental(monkeypatch,window={'start':date(2026,10,8),'end':date(2026,10,9)},free=2*1024**3)
    monkeypatch.setattr(library.shutil,'disk_usage',lambda _:N(free=3*1024**3))
    local.save=lambda **values:local.state.update(values)
    assert not local.can_continue()
    assert remote.can_continue()


def test_supplier_adapter_restores_all_old_windows_before_running_current_and_backfill(monkeypatch):
    from backtest_data import credentials
    from backtest_data.ingestion import http as http_module
    from backtest_data.prices.supplier_models import SupplierPriceCollectionState
    from backtest_data.prices.supplier_refresh_models import SupplierPriceRefreshState
    mod=api();scope=mod.full_manifest_scope(manifest());older={'start':date(2026,9,25),'end':date(2026,10,8)}
    backfill=mod.portable_supplier_state(supplier(scope),scope)
    old=mod.portable_supplier_state(supplier(scope,output_start=older['start'],output_end=older['end']),scope,requested_window=older)
    prior={'scope':scope,'version':2,'backfill':backfill,'refresh_windows':{mod._window_key(older):old},'coverage_complete':False}
    rows=[];calls=[];closed=[]
    class Session:
        def scalar(self,statement):
            if len(statement.selected_columns)==1:return None
            params=statement.compile().params
            for row in rows:
                if row.output_start==params.get('output_start_1') and row.output_end==params.get('output_end_1'):return row
            return None
        def add(self,row):rows.append(row)
        def flush(self):pass
    @contextmanager
    def txn(_):yield Session()
    monkeypatch.setattr(mod,'transaction',txn)
    monkeypatch.setattr(credentials,'load_credentials',lambda **_:N())
    monkeypatch.setattr(http_module,'ProviderHTTP',lambda _:N(close=lambda:closed.append('closed')))
    def collect(*args,**kwargs):
        assert len(rows)>=2
        window=kwargs['requested_window'];calls.append(window)
        assert 0<kwargs['budget_seconds']<=10
        if window and not any(row.output_start==window[0] and row.output_end==window[1] for row in rows):
            rows.append(SupplierPriceRefreshState(**vars(supplier(scope,output_start=window[0],output_end=window[1]))))
        return {'status':'budget_deferred','coverage_complete':False}
    monkeypatch.setattr(mod,'collect_supplier_adjusted',collect)
    state,outcome=mod.supplier_adjusted_step(N(),PathStub(),scope=scope,previous=prior,budget_seconds=10,as_of=NOW)
    assert calls==[(date(2026,9,26),date(2026,10,9)),(date(2026,9,25),date(2026,10,8)),None]
    assert isinstance(rows[0],SupplierPriceCollectionState) and isinstance(rows[1],SupplierPriceRefreshState)
    assert state['backfill']['cursor']['page_token'].value=='page-next'
    assert len(state['refresh_windows'])==2 and closed==['closed']
    assert len(outcome['window_outcomes'])==3
    dumps(state)


def test_supplier_recovery_keeps_committed_current_window_after_later_snapshot_failure(monkeypatch):
    mod=api();scope=mod.full_manifest_scope(manifest());window={'start':date(2026,9,26),'end':date(2026,10,9)}
    backfill=supplier(scope);refresh=supplier(scope,output_start=window['start'],output_end=window['end'])
    @contextmanager
    def txn(_):yield N(scalar=lambda _:backfill,scalars=lambda _:N(yield_per=lambda _:[refresh]))
    monkeypatch.setattr(mod,'transaction',txn)
    recovered=mod._recover_committed_state('supplier_adjusted',N(),scope,'work',None,'split')
    assert recovered['backfill']['cursor']['page_token'].value=='page-next'
    assert recovered['refresh_windows']['2026-09-26_2026-10-09']['cursor']['page_token'].value=='page-next'
    dumps(recovered)
