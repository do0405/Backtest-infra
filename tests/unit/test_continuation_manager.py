import json
from pathlib import Path


def fixture(tmp_path):
    from backtest_data.ops.continuation_manager import ContinuationManager
    from backtest_data.ops.continuation_profiles import profile
    root=tmp_path/'artifacts/continuous/runs/test';root.mkdir(parents=True)
    role='prices';p=root/'old.runtime.json';out=root/'old.stdout.json'
    p.write_text(json.dumps({'status':'running','finished_at':None,'exit_code':None}))
    out.write_text('{}')
    entry={'run_id':'old','pid':1,'runtime':str(p),'stdout':str(out),'run_directory':str(root)}
    meta={'run_id':'old','baseline_path':'unchanged','results':{role:entry},'auxiliary':{}}
    path=tmp_path/'artifacts/kr-full-workers.json';path.write_text(json.dumps(meta))
    class Host:
        observation='active'
        launch_count=0
        released=True
        def observe(self,entry,runtime):return self.observation
        def lock_released(self,role):return self.released
        def runnable(self,role):return True
        def free_space_ok(self):return True
        def launch(self,role,run):
            self.launch_count+=1
            (run/'initial-processes.json').write_text('{}')
            return {'ProcessId':10+self.launch_count,'ReturnValue':0,'identity_path':str(run/'initial-processes.json')}
    host=Host();clock=[100.]
    manager=ContinuationManager(tmp_path,root,host,roles=[role],clock=lambda:clock[0])
    return manager,host,p,out,path,clock


def test_adopts_existing_workers_without_restarting_or_changing_metadata(tmp_path):
    m,h,p,out,path,clock=fixture(tmp_path);before=path.read_bytes()
    for _ in range(8):m.tick();clock[0]+=30
    assert h.launch_count==0 and path.read_bytes()==before
    assert m.state['roles']['prices']['status']=='adopted_running'


def test_orphaned_live_child_and_domain_lock_withhold_replacement(tmp_path):
    m,h,p,out,path,clock=fixture(tmp_path)
    p.write_text(json.dumps({'status':'budget_deferred','finished_at':'date','exit_code':0}))
    out.write_text(json.dumps({'status':'budget_deferred'}));h.observation='orphan'
    m.tick();assert h.launch_count==0
    h.observation='gone';h.released=False;m.tick();clock[0]+=100;m.tick()
    assert h.launch_count==0


def test_budget_rollover_keeps_history_and_runs_one_child_per_generation(tmp_path):
    m,h,p,out,path,clock=fixture(tmp_path)
    p.write_text(json.dumps({'status':'budget_deferred','finished_at':'date','exit_code':0}))
    out.write_text(json.dumps({'status':'budget_deferred'}));h.observation='gone'
    m.tick();clock[0]+=6;m.tick()
    meta=json.loads(path.read_text());entry=meta['results']['prices']
    assert h.launch_count==1 and entry['pid']==11
    assert Path(entry['run_directory'],'previous-workers.json').is_file()
    assert meta['baseline_path']=='unchanged' and p.is_file() and out.is_file()
    # A new runtime is not yet written: never repeatedly launch in that window.
    m.tick();clock[0]+=60;m.tick();assert h.launch_count==1


def test_unknown_launch_outcome_survives_manager_restart_without_second_launch(tmp_path):
    from backtest_data.ops.continuation_manager import ContinuationManager
    m,h,p,out,path,clock=fixture(tmp_path)
    p.write_text(json.dumps({'status':'budget_deferred','finished_at':'date','exit_code':0}))
    out.write_text(json.dumps({'status':'budget_deferred'}));h.observation='gone'
    def uncertain(role,run):h.launch_count+=1;raise TimeoutError('uncertain')
    h.launch=uncertain;m.tick();clock[0]+=6;m.tick()
    assert m.state['roles']['prices']['requires_user']
    other=ContinuationManager(tmp_path,m.root,h,roles=['prices'],clock=lambda:clock[0])
    clock[0]+=100;other.tick();assert h.launch_count==1


def test_completed_and_identity_mismatch_never_launch(tmp_path):
    m,h,p,out,path,clock=fixture(tmp_path);h.observation='identity_mismatch'
    m.tick();assert m.state['roles']['prices']['requires_user'] and h.launch_count==0


def test_completed_generation_is_history_even_if_its_old_pid_is_reused(tmp_path):
    m,h,p,out,path,clock=fixture(tmp_path)
    p.write_text(json.dumps({'status':'partial','finished_at':'date','exit_code':0}))
    out.write_text(json.dumps({'status':'partial','blocked_sources':{}}));h.observation='gone'
    h.runnable=lambda role:False;m.tick()
    assert m.state['roles']['prices']['status']=='complete'
    h.observation='identity_mismatch';m.tick()
    assert m.state['roles']['prices']['status']=='complete' and not m.state['roles']['prices']['requires_user']


def test_credential_change_releases_auth_pause_without_touching_checkpoint(tmp_path):
    m,h,p,out,path,clock=fixture(tmp_path)
    p.write_text(json.dumps({'status':'blocked','finished_at':'date','exit_code':2}))
    out.write_text(json.dumps({'status':'blocked','reason':'401'}));h.observation='gone'
    marker=['old'];h.configuration_marker=lambda role:marker[0]
    m.tick();assert m.state['roles']['prices']['requires_user']
    marker[0]='new';m.tick();clock[0]+=6;m.tick()
    assert h.launch_count==1 and json.loads(out.read_text())['reason']=='401'


def test_finished_worker_without_valid_receipt_needs_review_instead_of_relaunch(tmp_path):
    m,h,p,out,path,clock=fixture(tmp_path)
    p.write_text(json.dumps({'status':'failed','finished_at':'date','exit_code':1}))
    out.write_text('');h.observation='gone'
    m.tick()
    assert m.state['roles']['prices']['requires_user'] and h.launch_count==0
