"""Singleton collection controller with an optional registered-start barrier."""
import argparse,msvcrt,os,sys,time
from pathlib import Path

WORKSPACE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(WORKSPACE/'src'))
from backtest_data.ops.continuation_manager import ContinuationManager,atomic_json,registration_admitted
from backtest_data.ops.continuation_host import WindowsHost


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-directory',type=Path,required=True)
    parser.add_argument('--refresh-manifest-id')
    parser.add_argument('--registration-ready',type=Path)
    args=parser.parse_args();run=args.run_directory.resolve();root=WORKSPACE/'artifacts/continuous'
    if not run.is_relative_to((root/'runs').resolve()) or not run.is_dir():raise ValueError('manager run outside registered root')
    if args.registration_ready and args.registration_ready.resolve()!=run/'registration-ready.json':
        raise ValueError('manager registration marker outside current run')
    lock=(root/'manager.lock').open('a+b')
    if lock.seek(0,2)==0:lock.write(b'0');lock.flush()
    lock.seek(0)
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        atomic_json(run/'duplicate-prevented.json',{'status':'duplicate_prevented','pid':os.getpid()});raise SystemExit(4)
    try:
        powershell=Path(r'C:\Users\pc01\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe')
        host=WindowsHost(WORKSPACE,root/'windows-host.ps1',powershell)
        manager=ContinuationManager(WORKSPACE,run,host,refresh_manifest_id=args.refresh_manifest_id)
        manager.state['manager_pid']=os.getpid();manager.save()
        if args.registration_ready:
            while not registration_admitted(args.registration_ready,run_id=run.name,manager_pid=os.getpid()):
                manager.state['status']='waiting_for_registration';manager.save();time.sleep(1)
        while True:
            try:
                host.refresh();manager.tick()
                if manager.state['status']=='collection_pass_complete':break
            except Exception as exc:
                manager.state.update(status='waiting_for_host_inventory',error_type=type(exc).__name__);manager.save()
            time.sleep(30)
    finally:
        lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1);lock.close()


if __name__=='__main__':main()
