"""Fresh hosted runner -> immutable published Release data and compact state."""
import argparse
from datetime import datetime
from functools import partial
import json
import os
from pathlib import Path
import shutil
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from backtest_data.db import make_engine,transaction,utcnow
from backtest_data.remote.bundle import digest,read_manifest
from backtest_data.remote.codec import unpack
from backtest_data.remote.folder import FolderTransport
from backtest_data.remote.planner import validate_ownership
from backtest_data.remote.releases import (github,inventory,download_asset,read_commit,verify_file,
    read_state,write_state,zip_store,release_tag,MAX_ASSET,source_status)
from backtest_data.remote.sql import import_bundle
from backtest_data.remote.worker import run_cycle
from backtest_data.remote.source_handlers import run_owned_handlers
from backtest_data.remote.archives import restore
from backtest_data.remote.identity import validate_raw_identity_index
from backtest_data.remote.quota import reserve_hosted_allowance,deferred_handler
from backtest_data.ingestion.quota import (install_quota_controls,upsert_quota_policy,
    grant_allocation_day_credits,set_provider_cooldown)
from backtest_data.ingestion.jobs import stable_hash

# Keep the existing 6000-second acquisition / 120-minute completion target.
# Export remains atomic and may still exceed this operational reservation.
EXPORT_RESERVE_SECONDS=1200
# The two-hour grace is publication-only; the immutable original quota
# expiry and the existing acquisition deadline never move later.
EXPECTED_COMPLETION_SECONDS=7200
PUBLICATION_TAIL_SECONDS=7200
HOSTED_KRX_BATCH_LIMITS={'max_requests':256,'max_new_rows':500_000,'max_raw_bytes':128*1024**2}


def main():
    started=time.monotonic();job_deadline=None
    def phase(name,**metrics):
        value={'status':'remote_phase','phase':name,'elapsed_seconds':round(time.monotonic()-started,2)}
        if job_deadline is not None:value['remaining_job_seconds']=round(max(0,job_deadline-time.monotonic()),2)
        value.update(metrics)
        print(json.dumps(value),flush=True)
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
    phase('bootstrap_restore_started')
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
    phase('bootstrap_restored')
    # This immutable control commit happens BEFORE provider HTTP. A crashed
    # hosted runner cannot make its allocated allowance available again.
    prior_clocks=[]
    allowance=reserve_hosted_allowance(gh,repo,{
        'stream_id':stream,'manifest_id':configuration['manifest_id'],
        'handoff_id':configuration['handoff_id'],'bootstrap_sha256':binding['sha256']},work/'quota',
        run_id=os.environ['GITHUB_RUN_ID'],run_attempt=int(os.environ.get('GITHUB_RUN_ATTEMPT','1')),
        budget_seconds=min(args.budget_seconds,HOSTED_KRX_BATCH_LIMITS['max_requests']*10),
        now=utcnow(),target=os.environ['GITHUB_SHA'],
        record_not_before=prior_clocks.append)
    acquisition_deadline=None
    if allowance is not None and allowance.get('expires_at'):
        expires_at=datetime.fromisoformat(allowance['expires_at'].replace('Z','+00:00'))
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:raise ValueError('aware hosted expiry required')
        quota_deadline=time.monotonic()+(expires_at-utcnow()).total_seconds()
        job_deadline=quota_deadline+PUBLICATION_TAIL_SECONDS
        acquisition_deadline=quota_deadline-EXPORT_RESERVE_SECONDS
    phase('quota_committed' if acquisition_deadline is not None else 'quota_deferred')
    engine=make_engine(os.environ['BACKTEST_DATABASE_URL'])
    raw=work/'raw'
    try:
        with transaction(engine) as session:
            install_quota_controls(session)
            upsert_quota_policy(session,'krx:shared_account',minimum_interval_seconds=10,
                window_kind='calendar_day',credit_limit=9500,require_allocation=True)
        def admit_rate(session):
            if acquisition_deadline is not None:
                grant_allocation_day_credits(session,'krx:shared_account',allowance['credits_by_day'],
                    binding_hash=stable_hash(allowance))
            if prior_clocks:set_provider_cooldown(session,'krx:shared_account',max(prior_clocks))
        phase('catalog_restore_started')
        for bundle in FolderTransport(work/'catalog').committed_bundles():
            with transaction(engine) as session:import_bundle(session,bundle,raw_root=raw)
        phase('catalog_restored')
        store=work/'delta-store';store.mkdir()
        if prior:
            (store/'heads').mkdir()
            (store/'heads'/f'{stream}.json').write_text(json.dumps(prior['head']),encoding='utf-8')
        collection_budget=min(args.budget_seconds,max(0,acquisition_deadline-time.monotonic())) if acquisition_deadline is not None else args.budget_seconds
        result=run_cycle(engine,raw,ownership=configuration,planned_jobs=[],incoming_bundles=[],
            output_root=work/'outgoing',transport_root=store,stream_id=stream,sequence=sequence,
            checkpoint=checkpoint,budget_seconds=collection_budget,max_private_bytes=MAX_ASSET,
            previous_bundle_id=previous,
            source_handlers=partial(run_owned_handlers,krx_batch_limits=HOSTED_KRX_BATCH_LIMITS) if acquisition_deadline is not None else deferred_handler,
            rate_admission=admit_rate,
            source_handler_budget_seconds=collection_budget,postprocess=None,
            acquisition_deadline=acquisition_deadline,phase_callback=phase)
        head=result['head'];last=store/'bundles'/head['bundle_id']
        state=unpack(read_manifest(last)['checkpoint'])
        state.update(handoff_id=configuration['handoff_id'],not_before=checkpoint.get('not_before'))
        # A separate compact state avoids restoring all past numeric observations
        # on each fresh runner. Immutable full deltas remain available to local SQL.
        write_state(work/'state.json.gz',state)
        counts=result['counts']
        phase('portable_state_written',bundle_count=result['delta_chunks'],row_count=sum(counts.values()),
              state_bytes=(work/'state.json.gz').stat().st_size)
        zip_store(store,work/'delta.zip')
        phase('archive_prepared',archive_bytes=(work/'delta.zip').stat().st_size)
        phase('commit_preparation_started')
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
        phase('commit_prepared')
        phase('release_upload_started')
        publication_timeout=max(1,int(job_deadline-time.monotonic())) if job_deadline is not None else PUBLICATION_TAIL_SECONDS
        github(gh,['release','create',tag,str(work/'delta.zip'),str(work/'state.json.gz'),str(commit_path),
            '--repo',repo,'--draft','--target',os.environ['GITHUB_SHA'],'--title','Data checkpoint '+stream+' '+str(head['sequence']),
            '--notes-file',str(notes)],timeout=publication_timeout)
        phase('release_uploaded')
        # Publication is the sole externally visible commit point. On upload
        # failure an unpublished draft cannot supersede the previous checkpoint.
        github(gh,['release','edit',tag,'--repo',repo,'--draft=false','--latest=false'])
        phase('release_published')
        print(json.dumps({'status':'remote_release_committed','tag':tag,'head':head,'counts':dict(counts),
            'unfinished_tasks':result['unfinished_tasks'],'original_target_count':17758,'coverage_complete':False}))
    finally:engine.dispose()


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'status':'remote_run_failed','error_type':type(exc).__name__}))
        raise SystemExit(2)
