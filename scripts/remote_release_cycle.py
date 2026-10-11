"""Fresh hosted runner -> immutable published Release data and compact state."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import shutil
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from backtest_data.db import make_engine,transaction,utcnow
from backtest_data.remote.bundle import digest,read_manifest,iter_bundle_rows
from backtest_data.remote.codec import loads
from backtest_data.remote.folder import FolderTransport
from backtest_data.remote.planner import validate_ownership
from backtest_data.remote.releases import (github,inventory,download_asset,read_commit,verify_file,
    read_state,write_state,zip_store,release_tag,MAX_ASSET,source_status)
from backtest_data.remote.sql import import_bundle
from backtest_data.remote.worker import run_cycle
from backtest_data.remote.source_handlers import run_owned_handlers
from backtest_data.remote.archives import restore
from backtest_data.remote.identity import validate_raw_identity_index


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--budget-seconds',type=int,default=6000)
    args=parser.parse_args()
    if not 30<=args.budget_seconds<=6000:raise ValueError('remote collection batch must allow checkpoint publication before runner timeout')
    repo=os.environ['REPOSITORY'];configuration=json.loads(os.environ['REMOTE_OWNERSHIP'])
    slots=validate_ownership(configuration)
    # Initial deployment deliberately admits only the independently fenced KRX
    # account. Other sources retain their live local collectors until separately
    # proven identity/task handoffs, never inferred from available credentials.
    if len(slots)!=1 or slots[0]['source']!='krx':raise ValueError('verified KRX-only deployment required')
    stream=configuration['owner_id'];binding=configuration['bootstrap']
    work=args.work.resolve();work.mkdir(parents=True,exist_ok=True)
    gh=shutil.which('gh')
    if not gh:raise ValueError('GitHub CLI required')
    bootstrap=json.loads(github(gh,['api',f"repos/{repo}/releases/tags/{binding['tag']}"]))
    if bootstrap.get('draft'):raise ValueError('committed catalog bootstrap required')
    archive=download_asset(gh,repo,bootstrap,'catalog.zip',work/'bootstrap-download')
    if digest(archive)!=binding['sha256']:raise ValueError('original bootstrap hash mismatch')
    restore(archive,work/'catalog',MAX_ASSET)
    checkpoint=read_state(download_asset(gh,repo,bootstrap,'initial-state.json.gz',work/'initial-download',max_bytes=128*1024**2))
    if digest(work/'initial-download/initial-state.json.gz')!=binding['state_sha256']:
        raise ValueError('original checkpoint hash mismatch')
    prior=None;prior_hash=None;sequence=1;previous=None
    releases=inventory(gh,repo,stream)
    if releases:
        release=releases[-1]
        commit_path=download_asset(gh,repo,release,'commit.json',work/'prior',max_bytes=1024**2)
        prior=read_commit(commit_path,stream=stream,bootstrap_hash=binding['sha256'],tag=release['tag_name'])
        state_path=download_asset(gh,repo,release,'state.json.gz',work/'prior')
        verify_file(state_path,prior['files']['state.json.gz']);checkpoint=read_state(state_path)
        sequence=prior['end_sequence']+1;previous=prior['head']['bundle_id'];prior_hash=digest(commit_path)
    if checkpoint.get('handoff_id')!=configuration['handoff_id']:
        raise ValueError('ownership checkpoint generation mismatch')
    validate_raw_identity_index(checkpoint['raw_identity_index'])
    if any(task['source']!='krx' or task['domain']!='market_quotes' or
           task['request'].get('manifest_id')!=configuration['manifest_id'] for task in checkpoint['task_state']):
        raise ValueError('original KRX task binding mismatch')
    if checkpoint.get('not_before') and utcnow()<checkpoint['not_before']:
        raise ValueError('prior account budget has not elapsed')
    engine=make_engine(os.environ['BACKTEST_DATABASE_URL'])
    raw=work/'raw'
    try:
        for bundle in FolderTransport(work/'catalog').committed_bundles():
            with transaction(engine) as session:import_bundle(session,bundle,raw_root=raw)
        store=work/'delta-store';store.mkdir()
        if prior:
            (store/'heads').mkdir()
            (store/'heads'/f'{stream}.json').write_text(json.dumps(prior['head']),encoding='utf-8')
        result=run_cycle(engine,raw,ownership=configuration,planned_jobs=[],incoming_bundles=[],
            output_root=work/'outgoing',transport_root=store,stream_id=stream,sequence=sequence,
            checkpoint=checkpoint,budget_seconds=args.budget_seconds,max_private_bytes=MAX_ASSET,
            previous_bundle_id=previous,source_handlers=run_owned_handlers,
            source_handler_budget_seconds=args.budget_seconds,postprocess=None)
        head=result['head'];last=store/'bundles'/head['bundle_id']
        state=loads(json.dumps(read_manifest(last)['checkpoint']))
        state.update(handoff_id=configuration['handoff_id'],not_before=checkpoint.get('not_before'))
        # A separate compact state avoids restoring all past numeric observations
        # on each fresh runner. Immutable full deltas remain available to local SQL.
        write_state(work/'state.json.gz',state)
        zip_store(store,work/'delta.zip')
        counts=Counter()
        for path in (store/'bundles').glob('*/manifest.json'):
            for row in iter_bundle_rows(path.parent):counts[row['table']]+=1
        commit={'format':'backtest_release_commit_v1','stream_id':stream,
            'start_sequence':sequence,'end_sequence':head['sequence'],'previous_bundle_id':previous,
            'head':head,'previous_tag':releases[-1]['tag_name'] if releases else None,
            'previous_commit_sha256':prior_hash,'bootstrap_sha256':binding['sha256'],
            'files':{name:{'bytes':(work/name).stat().st_size,'sha256':digest(work/name)} for name in ('delta.zip','state.json.gz')},
            'cloud_run_id':os.environ['GITHUB_RUN_ID'],'created_at':utcnow().isoformat(),
            'counts':dict(counts),'source_status':source_status(state.get('source_handler_outcomes',[])),'coverage_complete':False}
        commit_path=work/'commit.json';commit_path.write_text(json.dumps(commit,indent=2),encoding='utf-8')
        tag=release_tag(stream,head['sequence'],attempt=os.environ['GITHUB_RUN_ID']+'-a'+os.environ.get('GITHUB_RUN_ATTEMPT','1'))
        notes=work/'release-notes.txt';notes.write_text('Committed source observations and portable checkpoint. Original IDs, hashes and capture clocks preserved. Coverage is incomplete.',encoding='utf-8')
        github(gh,['release','create',tag,str(work/'delta.zip'),str(work/'state.json.gz'),str(commit_path),
            '--repo',repo,'--draft','--target',os.environ['GITHUB_SHA'],'--title','Data checkpoint '+stream+' '+str(head['sequence']),
            '--notes-file',str(notes)],timeout=300)
        # Publication is the sole externally visible commit point. On upload
        # failure an unpublished draft cannot supersede the previous checkpoint.
        github(gh,['release','edit',tag,'--repo',repo,'--draft=false','--latest=false'])
        print(json.dumps({'status':'remote_release_committed','tag':tag,'head':head,'counts':dict(counts),
            'unfinished_tasks':result['unfinished_tasks'],'original_target_count':17758,'coverage_complete':False}))
    finally:engine.dispose()


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'status':'remote_run_failed','error_type':type(exc).__name__}))
        raise SystemExit(2)
