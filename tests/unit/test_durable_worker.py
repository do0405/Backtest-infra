import json
from pathlib import Path
import sys

import pytest

pytestmark=pytest.mark.skipif(sys.platform!='win32',reason='Windows detached domain file lock')


def transient():
    return {'status':'blocked','reason':'OperationalError','database_error':{
        'category':'server_recovery','sqlstate':'57P03','retryable':True}}


def setup(tmp_path,kind='collection-watch'):
    root=tmp_path/'artifacts'/kind
    run=root/'runs'/'fixture'
    run.mkdir(parents=True)
    return root,run


def fake_children(monkeypatch,module,receipts,on_wait=lambda index:None):
    commands=[]
    class Child:
        def __init__(self,command,**kwargs):
            index=len(commands);commands.append(command)
            self.pid=10000+index;self.index=index
            code,receipt=receipts[index]
            self.code=code
            kwargs['stdout'].write(json.dumps(receipt).encode())
        def wait(self):on_wait(self.index);return self.code
    monkeypatch.setattr(module.subprocess,'Popen',Child)
    monkeypatch.setattr(module.time,'sleep',lambda seconds:None)
    return commands


def test_collector_retries_keep_separate_receipts_and_original_request(tmp_path,monkeypatch):
    from backtest_data.ops import durable_worker as module
    _,run=setup(tmp_path)
    commands=fake_children(monkeypatch,module,[(2,transient()),(0,{'status':'partial','coverage_complete':False})])
    assert module.collector_main(tmp_path,['--domain','prices','--market','both','--run-directory',str(run)])==0
    state=json.loads((run/'prices.runtime.json').read_text())
    assert commands[0]==commands[1]
    assert [item['exit_code'] for item in state['attempts']]==[2,0]
    assert state['status']=='partial' and state['coverage_complete'] is False
    assert state['attempts'][0]['stdout']!=state['attempts'][1]['stdout']
    assert json.loads(Path(state['attempts'][0]['stdout']).read_text())==transient()
    assert json.loads((run/'prices.stdout.json').read_text())['status']=='partial'


def test_provider_block_does_not_restart_collector(tmp_path,monkeypatch):
    from backtest_data.ops import durable_worker as module
    _,run=setup(tmp_path)
    commands=fake_children(monkeypatch,module,[(2,{'status':'blocked','reason':'401'})])
    assert module.collector_main(tmp_path,['--domain','fundamentals','--market','both','--run-directory',str(run)])==2
    state=json.loads((run/'fundamentals.runtime.json').read_text())
    assert len(commands)==1 and state['status']=='blocked'


def test_existing_runtime_is_not_overwritten_on_relaunch(tmp_path,monkeypatch):
    from backtest_data.ops import durable_worker as module
    _,run=setup(tmp_path)
    old=run/'prices.runtime.json';old.write_text('{"preserved":true}')
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**k:pytest.fail('must not spawn'))
    assert module.collector_main(tmp_path,['--domain','prices','--run-directory',str(run)])==9
    assert old.read_text()=='{"preserved":true}'


def test_publication_retry_cannot_reset_full_day_budget(tmp_path,monkeypatch):
    from backtest_data.ops import durable_worker as module
    root,run=setup(tmp_path,'public-pit')
    audit=root/'audit.json';audit.write_text('{}')
    progress=root/'progress.json';progress.write_text('{"cursor":"preserved"}')
    clock=[0.]
    commands=fake_children(monkeypatch,module,[(2,transient())],on_wait=lambda index:clock.__setitem__(0,86399.))
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(module.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    assert module.publication_main(tmp_path,['--run-directory',str(run),'--audit-path',str(audit),'--progress-path',str(progress)])==0
    assert len(commands)==1 and progress.read_text()=='{"cursor":"preserved"}'
    state=json.loads((run/'public_pit.runtime.json').read_text())
    assert state['status']=='budget_deferred' and state['coverage_complete'] is False


def test_running_domain_lock_prevents_second_wrapper(tmp_path,monkeypatch):
    import msvcrt
    from backtest_data.ops import durable_worker as module
    root,run=setup(tmp_path)
    with (root/'prices.runtime.lock').open('w+b') as lock:
        lock.write(b'0');lock.flush();lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        try:
            monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**k:pytest.fail('must not spawn'))
            assert module.collector_main(tmp_path,['--domain','prices','--run-directory',str(run)])==4
            values=list(run.glob('prices.duplicate-*.runtime.json'))
            assert len(values)==1 and json.loads(values[0].read_text())['status']=='duplicate_prevented'
        finally:lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)


def test_opt_in_lease_resume_keeps_domain_lock_and_attempt_receipts(tmp_path, monkeypatch):
    import msvcrt
    from backtest_data.ops import durable_worker as module
    root, run = setup(tmp_path)
    partial = {'status': 'partial', 'blocked_sources': {}, 'blocked_stages': [
        {'source': 'alpaca', 'stage': 'prices', 'reason': 'lease_lost'}], 'coverage_complete': False}
    commands = fake_children(monkeypatch, module, [(0, partial), (0, {'status': 'complete'})])

    def wait(seconds):
        state = json.loads((run/'prices.runtime.json').read_text())
        assert state['status'] == 'waiting_for_lease_resume'
        assert seconds == 60
        with (root/'prices.runtime.lock').open('r+b') as lock:
            lock.seek(0)
            with pytest.raises(OSError):
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)

    monkeypatch.setattr(module.time, 'sleep', wait)
    command = ['python', '-m', 'backtest_data', 'collect', '--source', 'alpaca']
    assert module.run_worker(tmp_path, root, run, 'prices', lambda remaining: command,
        lease_retry_delays=(60,)) == 0
    state = json.loads((run/'prices.runtime.json').read_text())
    assert commands == [command, command]
    assert state['recovery_policy'] == 'bounded_DB_connection_and_lease_loss_v2'
    assert state['status'] == 'complete' and len(state['attempts']) == 2
    assert json.loads(Path(state['attempts'][0]['stdout']).read_text()) == partial


def test_exhausted_lease_retries_remain_incomplete_and_explicit(tmp_path, monkeypatch):
    from backtest_data.ops import durable_worker as module
    root, run = setup(tmp_path)
    partial = {'status': 'partial', 'blocked_sources': {}, 'blocked_stages': [
        {'reason': 'lease_lost'}], 'coverage_complete': False}
    commands = fake_children(monkeypatch, module, [(0, partial), (0, partial)])
    assert module.run_worker(tmp_path, root, run, 'prices', lambda remaining: ['python'],
        lease_retry_delays=(60,)) == 0
    state = json.loads((run/'prices.runtime.json').read_text())
    assert len(commands) == 2 and state['status'] == 'lease_retry_exhausted'
    assert state['lease_retry_exhausted'] is True and state['coverage_complete'] is False
