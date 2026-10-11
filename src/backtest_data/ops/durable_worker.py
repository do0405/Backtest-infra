"""Windows detached workers with domain locks and preserved retry receipts."""
import argparse
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from .db_recovery import AttemptResult,DEFAULT_RETRY_DELAYS,run_with_database_recovery


def now():return datetime.now(timezone.utc).isoformat()


def receipt(path):
    if path.stat().st_size>1024*1024:return None
    try:
        value=json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value,dict) else None
    except (ValueError,OSError):return None


def run_worker(workspace,root,run,domain,command,*,total_budget=None,retry=True,extra=None,lease_retry_delays=()):
    import msvcrt
    target=run/(domain+'.runtime.json')
    if target.exists():return 9
    state={'runtime_schema_version':2,'domain':domain,'wrapper_pid':os.getpid(),'status':'starting',
        'started_at':now(),'child_pid':None,'child_spawned_at':None,'exit_code':None,'finished_at':None,
        'recovery_policy':'bounded_DB_connection_and_lease_loss_v2' if lease_retry_delays else 'bounded_DB_connection_only_v1',
        'max_attempts':1+(len(DEFAULT_RETRY_DELAYS) if retry else 0)+len(lease_retry_delays),
        'attempts':[],'coverage_complete':False,**(extra or {})}
    def save():
        pending=target.with_suffix('.tmp')
        pending.write_text(json.dumps(state,ensure_ascii=True,indent=2),encoding='utf-8')
        pending.replace(target)
    lock=(root/(domain+'.runtime.lock')).open('a+b')
    if lock.seek(0,2)==0:lock.write(b'0');lock.flush()
    lock.seek(0)
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        state.update(status='duplicate_prevented',exit_code=4,finished_at=now())
        duplicate=run/f'{domain}.duplicate-{os.getpid()}.runtime.json'
        with duplicate.open('x',encoding='utf-8') as handle:json.dump(state,handle,indent=2)
        lock.close();return 4
    if target.exists():
        lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1);lock.close();return 9
    output=run/(domain+'.stdout.json');error=run/(domain+'.stderr')
    result=AttemptResult(3,None)
    def execute(number,remaining):
        stdout=run/f'{domain}.attempt-{number:04d}.stdout.json'
        stderr=run/f'{domain}.attempt-{number:04d}.stderr'
        argv=command(remaining)
        item={'attempt':number,'started_at':now(),'command':argv,'stdout':str(stdout),'stderr':str(stderr),
            'remaining_budget_seconds':remaining,'child_pid':None,'child_spawned_at':None,'exit_code':None}
        state['attempts'].append(item)
        env=dict(os.environ);env['PYTHONIOENCODING']='utf-8'
        with stdout.open('xb') as out,stderr.open('xb') as err:
            child=subprocess.Popen(argv,cwd=workspace,env=env,stdout=out,stderr=err,
                creationflags=subprocess.CREATE_NO_WINDOW)
            item.update(child_pid=child.pid,child_spawned_at=now())
            state.update(status='running',current_attempt=number,child_pid=child.pid,
                child_spawned_at=item['child_spawned_at'],current_stdout=str(stdout),current_stderr=str(stderr),
                current_command=argv,next_retry_at=None)
            save()
            code=child.wait()
        item.update(exit_code=code,finished_at=now())
        value=receipt(stdout)
        item['result_status']=value.get('status') if value else None
        item['result_reason']=value.get('reason') if value else None
        # Keep immutable attempt receipts and a compatible final-result alias.
        shutil.copyfile(stdout,output);shutil.copyfile(stderr,error)
        save()
        return AttemptResult(code,value)
    def record(event):
        state['status']=event['status']
        state['recovery_event']=event
        if event['status'] in {'waiting_for_database','waiting_for_lease_resume'}:
            state['next_retry_at']=(datetime.now(timezone.utc)+timedelta(seconds=event['retry_in_seconds'])).isoformat()
        if event['status']=='database_retry_exhausted':state['database_retry_exhausted']=True
        if event['status']=='lease_retry_exhausted':state['lease_retry_exhausted']=True
        save()
    try:
        output.touch();error.touch()
        if lease_retry_delays:
            from .checkpoint_recovery import run_with_checkpoint_recovery
            result=run_with_checkpoint_recovery(execute,database_retry_delays=DEFAULT_RETRY_DELAYS if retry else (),
                lease_retry_delays=lease_retry_delays,total_budget_seconds=total_budget,
                sleep=time.sleep,monotonic=time.monotonic,record=record)
        else:
            result=run_with_database_recovery(execute,retry_delays=DEFAULT_RETRY_DELAYS if retry else (),
                total_budget_seconds=total_budget,sleep=time.sleep,monotonic=time.monotonic,record=record)
        if result.exit_code==0:
            state['status']=result.receipt.get('status','result_unconfirmed') if result.receipt else 'result_unconfirmed'
            if state.get('lease_retry_exhausted'):state['status']='lease_retry_exhausted'
            if result.receipt and result.receipt.get('reason') in {'database_recovery_budget_exhausted','checkpoint_recovery_budget_exhausted'}:
                output.write_text(json.dumps(result.receipt),encoding='utf-8')
        else:state['status']='blocked' if result.exit_code==2 else 'failed'
    except Exception as exc:
        state.update(status='wrapper_failed',error_type=type(exc).__name__)
        result=AttemptResult(3,None)
    finally:
        state.update(exit_code=result.exit_code,finished_at=now(),next_retry_at=None)
        try:save()
        finally:
            lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1);lock.close()
    return result.exit_code


def collector_main(workspace,argv=None):
    workspace=Path(workspace).resolve();root=workspace/'artifacts'/'collection-watch'
    parser=argparse.ArgumentParser()
    parser.add_argument('--domain',choices=['prices','fundamentals','price_recovery'],required=True)
    parser.add_argument('--market',choices=['kr','us','both'],default='kr')
    parser.add_argument('--run-directory',type=Path,required=True)
    args=parser.parse_args(argv);run=args.run_directory.resolve()
    if not run.is_relative_to((root/'runs').resolve()) or not run.is_dir():return 9
    command=[str(workspace/'.venv'/'Scripts'/'python.exe'),'-m','backtest_data','collect',
        '--market',args.market,'--domain',args.domain,'--mode','incremental']
    if args.domain=='price_recovery':
        command=[str(workspace/'.venv'/'Scripts'/'python.exe'),'-m','backtest_data','maintenance','replay-kis-prices',
            '--audit-path',str(workspace/'artifacts'/'kr-price-validation-audit.json'),
            '--progress-path',str(workspace/'artifacts'/'kr-price-recovery-progress.json'),'--requeue-recovered']
    return run_worker(workspace,root,run,args.domain,lambda remaining:command,
        retry=args.domain!='price_recovery',extra={'market':args.market})


def publication_main(workspace,argv=None):
    workspace=Path(workspace).resolve();root=workspace/'artifacts'/'public-pit'
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-directory',type=Path,required=True)
    parser.add_argument('--audit-path',type=Path,required=True)
    parser.add_argument('--progress-path',type=Path,required=True)
    args=parser.parse_args(argv);run=args.run_directory.resolve()
    audit=args.audit_path.resolve();progress=args.progress_path.resolve()
    if not run.is_relative_to((root/'runs').resolve()) or not run.is_dir():return 9
    if not audit.is_relative_to(root.resolve()) or not progress.is_relative_to(root.resolve()) or not audit.is_file():return 9
    def command(remaining):
        return [str(workspace/'.venv'/'Scripts'/'python.exe'),'-m','backtest_data','publication','verify',
            '--audit-path',str(audit),'--progress-path',str(progress),'--budget-seconds',str(remaining),
            '--batch-size','5','--archive-compressed-mb','256','--archive-total-mb','2048']
    return run_worker(workspace,root,run,'public_pit',command,total_budget=86400,
        extra={'audit_path':str(audit),'progress_path':str(progress),'budget_seconds':86400,
            'batch_size':5,'archive_compressed_mb':256,'archive_total_mb':2048})
