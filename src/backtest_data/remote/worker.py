"""Dedicated remote entry point; no .env discovery and no ownership defaults."""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from sqlalchemy import select
from ..db import make_engine, transaction
from ..ingestion.jobs import logical_request, plan_job, stable_hash
from ..ingestion.models import CollectionJob, TargetUniverseManifest
from .bundle import read_manifest
from .codec import dumps, loads
from .checkpoint import restore_tasks, snapshot_jobs, refresh_watermarks
from .folder import FolderTransport
from .planner import validate_ownership,CORE_SOURCES
from .registry import tables
from .sql import export_deltas, import_bundle
from .identity import restore_raw_identities, snapshot_raw_identities


def run_cycle(engine, raw_root, *, ownership, planned_jobs, incoming_bundles, output_root,
              transport_root, stream_id, sequence, checkpoint=None, budget_seconds=600,
              max_private_bytes=8 * 1024 ** 3, previous_bundle_id=None,
              postprocess=None, postprocess_budget_seconds=60,
              source_handlers=None,source_handler_budget_seconds=120,rate_admission=None,
              acquisition_deadline=None,phase_callback=None):
    slots = validate_ownership(ownership)
    if not 0 <= budget_seconds <= 14400 or budget_seconds==0 and acquisition_deadline is None:
        raise ValueError("bounded remote execution window required")
    if acquisition_deadline is not None and not math.isfinite(acquisition_deadline):
        raise ValueError('finite acquisition deadline required')
    def phase(name):
        if phase_callback is not None:phase_callback(name)
    core_slots=[s for s in slots if s['source'] in CORE_SOURCES and s.get('handler')!='supplier_adjusted']
    reserved_post=min(postprocess_budget_seconds,budget_seconds/5) if postprocess else 0
    handler_ceiling=budget_seconds/3 if core_slots else budget_seconds-reserved_post
    reserved_handlers=min(source_handler_budget_seconds,handler_ceiling) if source_handlers else 0
    if reserved_post<0 or reserved_handlers<0:raise ValueError('bounded processing reservation required')
    deadline = time.monotonic() + budget_seconds; checkpoint = dict(checkpoint or {})
    if acquisition_deadline is not None:deadline=min(deadline,acquisition_deadline)
    completed = set(checkpoint.get("completed_task_ids", [])); registry = tables()
    phase('operational_restore_started')
    for path in incoming_bundles:
        with transaction(engine) as session: import_bundle(session, path, raw_root=raw_root)
    with transaction(engine) as session:
        if 'raw_identity_index' in checkpoint:
            restore_raw_identities(session, checkpoint['raw_identity_index'], authority='new_remote_ephemeral_database')
        restore_tasks(session,checkpoint.get('task_state',[]),owned_sources={s['source'] for s in slots},authority='new_remote_ephemeral_database')
        from ..ingestion.quota import restore_rate_state
        rate_binding={'owner_id':ownership['owner_id'],'manifest_id':ownership['manifest_id']}
        if 'provider_rate_state' in checkpoint:
            restore_rate_state(session,checkpoint['provider_rate_state'],
                sources={s['source'] for s in slots},binding=rate_binding)
        # A remote quota allocation must be externally committed before this
        # callback can grant its credits. Restored old allocations never suffice.
        if rate_admission is not None:rate_admission(session)
    phase('operational_restored')
    # Commit portable queue restoration before dynamic discovery/planning reads.
    # The planner may return new current-day work while old jobs remain pending.
    if callable(planned_jobs):planned_jobs=planned_jobs(checkpoint)
    with transaction(engine) as session:
        from ..ingestion.quota import snapshot_rate_state
        manifest = session.get(TargetUniverseManifest, ownership.get("manifest_id"))
        if manifest is None or len(manifest.members) != 17758: raise ValueError("whole target bootstrap manifest required")
        scope = {"manifest_id": manifest.id, "target_count": 17758,
                 "output_start": manifest.scope["output_start"], "output_end": manifest.scope["output_end"]}
        if scope["output_start"] != "2016-01-01" or scope["output_end"] != "2026-10-07": raise ValueError("original backfill scope must remain intact")
        before = {name: set(session.scalars(select(list(table.primary_key.columns)[0]))) for name, table in registry.items()}
        task_jobs = {}
        for spec in planned_jobs:
            dumps(spec)
            if not any(s["source"] == spec["source"] and s["domain"] == ("prices" if spec["domain"] in {'prices','market_quotes'} else "fundamentals") for s in slots):
                raise ValueError("planned task outside explicitly owned source slot")
            identity = stable_hash([spec["source"], spec["domain"], spec["entity_id"], logical_request(spec["request"])])
            if identity in completed: continue
            job = plan_job(session, source=spec["source"], domain=spec["domain"], entity_id=spec["entity_id"], request=spec["request"], priority=spec.get("priority",10))
            task_jobs[identity] = job.id
    outcomes = []
    phase('acquisition_started')
    from ..ingestion.supervisor import run_collection
    for position,slot in enumerate(core_slots):
        remaining = (deadline - time.monotonic() - reserved_post - reserved_handlers)/(len(core_slots)-position)
        if remaining <= 0: break
        outcomes.append(run_collection(engine, raw_root, market=slot["market"], domain=slot["domain"],
                                       budget_seconds=remaining, mode="incremental", source_filter=[slot["source"]]))
    postprocess_state={}
    if source_handlers and deadline-time.monotonic()>reserved_post:
        postprocess_state=source_handlers(engine,raw_root,manifest_id=scope['manifest_id'],slots=slots,
            owned_sources={s['source'] for s in slots},checkpoint=checkpoint,
            budget_seconds=min(reserved_handlers,deadline-time.monotonic()-reserved_post),work_root=output_root,
            authority='new_remote_ephemeral_database',
            **({'deadline':deadline-reserved_post} if acquisition_deadline is not None else {}))
        if not isinstance(postprocess_state,dict):raise ValueError('safe handler checkpoint mapping required')
        dumps(postprocess_state)
    if postprocess and deadline>time.monotonic():
        next_state=postprocess(engine,manifest_id=scope['manifest_id'],owned_sources={s['source'] for s in slots},
            checkpoint=checkpoint,budget_seconds=min(reserved_post,deadline-time.monotonic()),work_root=output_root)
        if not isinstance(next_state,dict):raise ValueError('safe postprocess checkpoint mapping required')
        postprocess_state.update(next_state)
        dumps(postprocess_state)
    phase('acquisition_completed')
    phase('checkpoint_snapshot_started')
    with transaction(engine) as session:
        for identity, job_id in task_jobs.items():
            job = session.get(CollectionJob, job_id)
            if job.status == "succeeded": completed.add(identity)
        # Keep safe typed pagination and child work; operational/auth rows and
        # ownership/lease/fencing authority never enter the payload.
        all_owned=list(session.scalars(select(CollectionJob).where(CollectionJob.source.in_({s['source'] for s in slots}))))
        portable=snapshot_jobs(all_owned,owned_sources={s['source'] for s in slots})
        durable_checkpoint = {**{k:v for k,v in checkpoint.items() if k in {'daily_publication_progress','daily_publication_status','source_handler_state','source_handler_outcomes'}},**postprocess_state,
                              "version": 2, "completed_task_ids": sorted(completed), "task_state":portable,
                              "provider_rate_state":snapshot_rate_state(session,{s['source'] for s in slots},binding=rate_binding),
                              **({'raw_identity_index':snapshot_raw_identities(session)} if 'raw_identity_index' in checkpoint else {}),
                              "unfinished_task_ids": sorted(j.identity_hash for j in all_owned if j.status not in {'succeeded','superseded'}),
                              "completed_through":refresh_watermarks(all_owned,checkpoint.get('completed_through',{}),owned_sources={s['source'] for s in slots}),
                              "data_export_cursor":{"stream_id":stream_id,"sequence":sequence,"previous_bundle_id":previous_bundle_id},
                              "coverage_complete": False}
        roots = {name: sorted(set(session.scalars(select(list(table.primary_key.columns)[0]))) - before[name]) for name, table in registry.items()}
        phase('checkpoint_snapshot_completed')
        phase('checkpoint_export_started')
        paths = export_deltas(session, roots, output_root, stream_id=stream_id, sequence=sequence,
                            scope=scope, checkpoint=durable_checkpoint, raw_root=raw_root,previous_bundle_id=previous_bundle_id)
    phase('checkpoint_export_completed')
    phase('folder_publication_started')
    head = FolderTransport(transport_root, max_retained_bytes=max_private_bytes).publish_group(paths)
    phase('folder_publication_completed')
    return {"status": "private_delta_committed", "head": head, "acquisition_runs": len(outcomes),
            "unfinished_tasks": len(durable_checkpoint["unfinished_task_ids"]),"delta_chunks":len(paths), "coverage_complete": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Private bounded remote source acquisition")
    parser.add_argument("--ownership", type=Path, required=True)
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--incoming", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-store", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--stream-id", required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--budget-seconds", type=float, default=600)
    args = parser.parse_args(argv)
    ownership = json.loads(args.ownership.read_text()); validate_ownership(ownership)
    # URL is provided only through the process environment; never bundle/log it.
    import os
    engine = make_engine(os.environ["BACKTEST_DATABASE_URL"])
    try:
        if (args.incoming/'heads').is_dir():
            incoming=FolderTransport(args.incoming).committed_bundles()
        else:
            incoming=sorted((p.parent for p in args.incoming.glob('*/manifest.json')),
                key=lambda path:(read_manifest(path)['stream_id'],read_manifest(path)['sequence']))
        from .source_handlers import run_owned_handlers
        from .postprocess import daily_publication_step
        result = run_cycle(engine, args.raw_root, ownership=ownership, planned_jobs=loads(args.jobs.read_text()),
            incoming_bundles=incoming, output_root=args.output,
            transport_root=args.private_store, stream_id=args.stream_id, sequence=args.sequence,
            checkpoint=loads(args.checkpoint.read_text()) if args.checkpoint else None, budget_seconds=args.budget_seconds,
            source_handlers=run_owned_handlers,postprocess=daily_publication_step)
        print(json.dumps(result))
    finally: engine.dispose()
    return 0


if __name__ == "__main__": raise SystemExit(main())
