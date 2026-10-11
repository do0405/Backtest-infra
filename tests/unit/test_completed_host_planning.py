from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace as N

from backtest_data import db,remote_updates
from backtest_data.ingestion import jobs as library
from backtest_data.ops.continuation_host import WindowsHost


def run_plan(monkeypatch,*,outstanding=False):
    members=[{'security_id':str(i),'symbol':'AAPL','market':'US'} for i in range(251)]
    scope={'output_start':'2016-01-01','output_end':'2026-10-07'}
    target=N(id='whole',content_hash='a'*64,scope=scope,
        members=[{'market':'KR'} for _ in range(4507)]+[{'market':'US'} for _ in range(13251)])
    roots=[N(source='alpaca',domain='prices',entity_id=m['security_id'],status='succeeded',
        request={**scope,'manifest_id':'whole'}) for m in members]
    calls=[]
    class Session:
        def get(self,*_):return target
        def execute(self,statement):calls.append(str(statement))
        def scalars(self,statement):return roots
        def scalar(self,statement):
            values=statement.compile().params
            if 'bool_and' in str(statement):
                assert 'count(distinct' in str(statement)
                assert 'original_backfill_end' in values.values()
                return None
            if values.get('status_1')==['pending','retry','running']:return 'pending-job' if outstanding else None
            return None
    @contextmanager
    def transaction(_):
        calls.append('begin');yield Session();calls.append('commit')
    monkeypatch.setattr(db,'transaction',transaction)
    monkeypatch.setattr(remote_updates,'load_update_context',lambda *_args,**_kwargs:(members,scope,[],{}))
    monkeypatch.setattr(library,'plan_job',lambda *_args,**kwargs:calls.append(kwargs))
    h=WindowsHost('unused','unused','unused');h.engine=N()
    result=h._plan_completed_refresh_locked('alpaca_prices','whole',None,datetime(2026,10,13,10,tzinfo=timezone.utc))
    return result,calls


def test_host_returns_planning_cursor_only_after_bounded_job_commit(monkeypatch):
    result,calls=run_plan(monkeypatch)
    assert result['status']=='planning' and result['checkpoint']['cursor']==250
    specs=[call for call in calls if isinstance(call,dict)]
    assert len(specs)==250 and all(spec['source']=='alpaca' for spec in specs)
    assert calls[-1]=='commit' and 'jobs' not in result and result['coverage_complete'] is False


def test_host_admits_pending_refresh_retry_without_recreating_or_resetting_jobs(monkeypatch):
    result,calls=run_plan(monkeypatch,outstanding=True)
    assert result['status']=='ready' and not any(isinstance(call,dict) for call in calls)
    assert calls[-1]=='commit' and result['coverage_complete'] is False
