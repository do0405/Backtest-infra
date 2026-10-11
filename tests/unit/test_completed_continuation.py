import json
from datetime import datetime, timezone
from pathlib import Path

from backtest_data.ops.continuation_manager import ContinuationManager


def manager(tmp_path,role='alpaca_prices'):
    root=tmp_path/'artifacts/continuous/runs/new';root.mkdir(parents=True)
    runtime=root/'old.runtime.json';runtime.write_text(json.dumps({'finished_at':'old','exit_code':0}))
    stdout=root/'old.stdout.json';stdout.write_text(json.dumps({'status':'partial','blocked_sources':{}}))
    section='results' if role in {'prices','fundamentals'} else 'auxiliary'
    entry={'pid':1,'runtime':str(runtime),'stdout':str(stdout),'run_directory':str(root)}
    metadata=tmp_path/'artifacts/kr-full-workers.json'
    metadata.write_text(json.dumps({'results':{},'auxiliary':{},section:{role:entry}}))
    class Host:
        calls=[]
        result={'status':'up_to_date','checkpoint':{},'coverage_complete':False}
        def observe(self,*_):return 'gone'
        def runnable(self,*_):return False
        def free_space_ok(self):return True
        def lock_released(self,*_):return True
        def plan_completed_refresh(self,role,entry,**kwargs):
            self.calls.append(kwargs);return self.result
        def launch(self,role,run):
            self.calls.append('launch')
            return {'ProcessId':10,'ReturnValue':0,'identity_path':str(run/'identity.json')}
    host=Host();clock=[100.]
    try:
        m=ContinuationManager(tmp_path,root,host,roles=[role],clock=lambda:clock[0],refresh_manifest_id='whole')
    except TypeError:
        import pytest
        pytest.fail('completed manager cadence hook is required')
    return m,host,clock,metadata,entry


def test_complete_role_keeps_daily_manager_alive_and_plans_under_host_admission(tmp_path):
    m,h,clock,metadata,entry=manager(tmp_path);before=metadata.read_bytes()
    m.tick();m.tick()
    assert h.calls and h.calls[0]['manifest_id']=='whole'
    assert m.state['roles']['alpaca_prices']['status']=='waiting_for_publication'
    assert m.state['status']=='managing' and metadata.read_bytes()==before


def test_refresh_planning_is_bounded_and_launch_waits_for_committed_complete_plan(tmp_path):
    m,h,clock,metadata,entry=manager(tmp_path);m.tick()
    h.result={'status':'planning','checkpoint':{'cursor':250},'coverage_complete':False}
    m.tick();assert 'launch' not in h.calls
    h.result={'status':'ready','checkpoint':{'cursor':500},'coverage_complete':False}
    m.tick();clock[0]+=6;m.tick()
    assert h.calls.count('launch')==1
    assert m.state['roles']['alpaca_prices']['refresh_planning']['cursor']==500
    m.tick();assert h.calls.count('launch')==1


def test_operator_stopped_local_krx_is_never_reinterpreted_as_completion(tmp_path):
    m,h,clock,metadata,entry=manager(tmp_path,'krx_quotes')
    m.state['roles']['krx_quotes']={'entry_runtime':entry['runtime'],'status':'operator_stopped'}
    for _ in range(3):m.tick()
    assert h.calls==[] and m.state['roles']['krx_quotes']['status']=='operator_stopped'


def test_completed_role_operator_stop_blocks_planner_and_launch(tmp_path):
    m,h,clock,metadata,entry=manager(tmp_path)
    stop=Path(entry['run_directory'])/'stop.json';stop.write_text(json.dumps({'operator_stop':True}))
    meta=json.loads(metadata.read_text());meta['auxiliary']['alpaca_prices']['operator_stop_path']=str(stop)
    metadata.write_text(json.dumps(meta));m.state['roles']['alpaca_prices']={'entry_runtime':entry['runtime'],'status':'complete'}
    m.tick();assert h.calls==[] and m.state['roles']['alpaca_prices']['status']=='operator_stopped'


def test_host_completed_planner_holds_existing_lock_and_rejects_another_source_owner(tmp_path,monkeypatch):
    from backtest_data.ops.continuation_host import WindowsHost
    h=WindowsHost(tmp_path,tmp_path/'helper','powershell');h.ready=True
    h.processes={99:{'ProcessId':99,'ParentProcessId':98,'CommandLine':
        'python -m backtest_data collect --market us --domain prices --source alpaca'}}
    try:
        result=h.plan_completed_refresh('alpaca_prices',{},manifest_id='whole',previous=None,
            as_of=datetime(2026,10,12,tzinfo=timezone.utc))
    except AttributeError:
        import pytest
        pytest.fail('completed host admission hook is required')
    assert result['status']=='existing_owner'
    assert not (tmp_path/'artifacts/collection-watch/alpaca_prices.runtime.lock').exists()


def test_host_rechecks_process_inventory_while_holding_the_original_lock(tmp_path,monkeypatch):
    import msvcrt
    from backtest_data.ops.continuation_host import WindowsHost
    role='alpaca_prices';root=tmp_path/'artifacts/collection-watch';root.mkdir(parents=True)
    metadata=tmp_path/'artifacts/kr-full-workers.json';entry={'runtime':'completed'}
    metadata.write_text(json.dumps({'auxiliary':{role:entry}}))
    h=WindowsHost(tmp_path,tmp_path/'helper','powershell');h.ready=True
    monkeypatch.setattr(h,'free_space_ok',lambda:True)
    calls=[]
    def refreshed():
        with (root/(role+'.runtime.lock')).open('r+b') as other:
            other.seek(0)
            import pytest
            with pytest.raises(OSError):msvcrt.locking(other.fileno(),msvcrt.LK_NBLCK,1)
        calls.append('fresh_inventory')
        h.processes={99:{'CommandLine':'python -m backtest_data collect --market us --domain prices --source alpaca'}}
    monkeypatch.setattr(h,'refresh',refreshed)
    monkeypatch.setattr(h,'_plan_completed_refresh_locked',lambda *_:calls.append('plan'))
    result=h.plan_completed_refresh(role,entry,manifest_id='whole',previous=None,
        as_of=datetime(2026,10,12,tzinfo=timezone.utc))
    assert result['status']=='existing_owner' and calls==['fresh_inventory']
    with (root/(role+'.runtime.lock')).open('r+b') as other:
        other.seek(0);msvcrt.locking(other.fileno(),msvcrt.LK_NBLCK,1)
        other.seek(0);msvcrt.locking(other.fileno(),msvcrt.LK_UNLCK,1)


def test_host_honors_operator_stop_added_after_manager_observation(tmp_path,monkeypatch):
    from backtest_data.ops.continuation_host import WindowsHost
    role='alpaca_prices';root=tmp_path/'artifacts/collection-watch';root.mkdir(parents=True)
    metadata=tmp_path/'artifacts/kr-full-workers.json';entry={'runtime':'completed'}
    stop=tmp_path/'stop.json';stop.write_text(json.dumps({'operator_stop':True}))
    metadata.write_text(json.dumps({'auxiliary':{role:{**entry,'operator_stop_path':str(stop)}}}))
    h=WindowsHost(tmp_path,tmp_path/'helper','powershell');h.ready=True
    monkeypatch.setattr(h,'free_space_ok',lambda:True);monkeypatch.setattr(h,'refresh',lambda:None)
    monkeypatch.setattr(h,'_plan_completed_refresh_locked',lambda *_:{'status':'ready'})
    result=h.plan_completed_refresh(role,entry,manifest_id='whole',previous=None,
        as_of=datetime(2026,10,12,tzinfo=timezone.utc))
    assert result['status']=='operator_stopped'
