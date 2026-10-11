"""Singleton local Release poller; atomic SQL import and restartable receipts."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from backtest_data.config import Settings
from backtest_data.db import make_engine,utcnow
from backtest_data.ops.continuation_manager import atomic_json
from backtest_data.remote.releases import (inventory,download_asset,read_commit,verify_file,digest,
    import_release,MAX_ASSET)
from backtest_data.remote.archives import restore


def lock_file(path):
    file=Path(path).open('a+b')
    if file.seek(0,2)==0:file.write(b'0');file.flush()
    file.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:file.close();raise RuntimeError('duplicate_sync_prevented') from None
    return file


def configure_github(configuration):
    """Bind the already-authorized CLI config across Windows app namespaces."""
    value=configuration.get('github_config_directory')
    if value is None:return
    path=Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError('existing_github_configuration_required')
    # Only the directory is passed to the official CLI; credentials are never
    # copied, read into this process, printed or included in transfer data.
    os.environ['GH_CONFIG_DIR']=str(path)


def sync_once(engine,gh,configuration,root,state):
    repo=configuration['repository'];stream=configuration['owner_id'];binding=configuration['bootstrap']
    if shutil.disk_usage(root).free<50*1024**3:raise RuntimeError('storage_space_required')
    releases=inventory(gh,repo,stream)
    for release in releases:
        completed=state.get('completed_releases',{})
        tag=release['tag_name']
        if tag in completed:continue
        run=Path(root)/'downloads'/f'{tag}-{uuid4().hex[:10]}'
        commit_path=download_asset(gh,repo,release,'commit.json',run,max_bytes=1024**2)
        commit=read_commit(commit_path,stream=stream,bootstrap_hash=binding['sha256'],tag=tag)
        if commit['start_sequence']!=state.get('end_sequence',0)+1 or commit['previous_tag']!=state.get('last_tag') or commit['previous_commit_sha256']!=state.get('last_commit_sha256'):
            raise ValueError('remote_checkpoint_lineage_requires_review')
        archive=download_asset(gh,repo,release,'delta.zip',run)
        verify_file(archive,commit['files']['delta.zip'])
        store=run/'store';restore(archive,store,MAX_ASSET)
        result=import_release(engine,store,commit,Settings.from_env().raw_root)
        # A receipt on disk is recorded after all SQL transactions commit. If
        # this replace is interrupted, replay inserts zero rather than guessing.
        atomic_json(run/'sql-import.json',result)
        completed[tag]={'commit_sha256':digest(commit_path),'checked_at':utcnow().isoformat(),
            'cloud_run_id':commit['cloud_run_id'],'inserted_rows':result['inserted_rows'],
            'counts':commit['counts'],'receipt_path':str(run/'sql-import.json')}
        state.update(completed_releases=completed,end_sequence=commit['end_sequence'],last_tag=tag,
            last_commit_sha256=digest(commit_path),last_commit_at=utcnow().isoformat(),
            source_status=commit['source_status'],inserted_rows=state.get('inserted_rows',0)+result['inserted_rows'])
        atomic_json(Path(root)/'progress.json',state)
    actionable=any(v['requires_user'] for v in state.get('source_status',[]))
    for field in ('error_type','sqlstate'):
        state.pop(field,None)
    state.update(checked_at=utcnow().isoformat(),status='source_action_required' if actionable else 'watching_committed_releases',requires_user=actionable,
        repository=repo,stream_id=stream,coverage_complete=False)
    atomic_json(Path(root)/'progress.json',state)
    return state


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--configuration',type=Path,required=True)
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--once',action='store_true')
    parser.add_argument('--interval-seconds',type=int,default=300)
    args=parser.parse_args()
    if not 30<=args.interval_seconds<=3600:raise ValueError('bounded sync poll interval required')
    root=args.root.resolve();root.mkdir(parents=True,exist_ok=True)
    lock=lock_file(root/'sync.lock')
    gh=shutil.which('gh') or str(Path(__file__).resolve().parents[1]/'artifacts/github-setup/cli/bin/gh.exe')
    settings=Settings.from_env();engine=make_engine(settings.database_url)
    path=root/'progress.json';state=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    runtime=root/'runtime.json'
    atomic_json(runtime,{'status':'running','pid':os.getpid(),'started_at':utcnow().isoformat(),
        'command':sys.argv,'policy':'continuous_release_import_v1','coverage_complete':False})
    try:
        while True:
            try:
                from backtest_data.ops.continuation_manager import read_json
                config=read_json(args.configuration)
                configure_github(config)
                sync_once(engine,gh,config,root,state)
            except Exception as exc:
                sqlstate=getattr(getattr(exc,'orig',None),'sqlstate',None)
                temporary_sql=bool(sqlstate and (sqlstate.startswith('08') or sqlstate in {'57P01','57P02','57P03'}))
                statement_timeout=sqlstate=='57014' and 'statement timeout' in str(getattr(exc,'orig','')).lower()
                transient_file=isinstance(exc,(OSError,json.JSONDecodeError))
                permanent=(isinstance(exc,ValueError) and not transient_file or
                    isinstance(exc,RuntimeError) and str(exc)=='storage_space_required' or
                    sqlstate is not None and not temporary_sql and not statement_timeout)
                state.update(checked_at=utcnow().isoformat(),status='action_required' if permanent else 'waiting_for_sync_retry',
                    error_type=type(exc).__name__,sqlstate=sqlstate,requires_user=permanent)
                atomic_json(path,state)
                if args.once:raise
            if args.once:print(json.dumps({k:state.get(k) for k in ('status','last_tag','end_sequence','inserted_rows','requires_user')}));return
            time.sleep(args.interval_seconds)
    finally:
        atomic_json(runtime,{'status':'finished','pid':os.getpid(),'finished_at':utcnow().isoformat(),
            'policy':'continuous_release_import_v1','coverage_complete':False})
        engine.dispose();lock.close()


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'status':'sync_failed','error_type':type(exc).__name__}))
        raise SystemExit(2)
