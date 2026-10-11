"""Acquire from restored private state using the existing source job planner."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backtest_data.db import make_engine, transaction
from backtest_data.remote.bundle import read_manifest, safe_path
from backtest_data.remote.codec import loads
from backtest_data.remote.checkpoint import catchup_since
from backtest_data.remote.planner import validate_ownership,CORE_SOURCES
from backtest_data.remote.postprocess import daily_publication_step
from backtest_data.remote.sql import import_bundle
from backtest_data.remote.worker import run_cycle
from backtest_data.remote.folder import FolderTransport


def main():
    p=argparse.ArgumentParser();p.add_argument("--ownership",type=Path,required=True);p.add_argument("--private-store",type=Path,required=True)
    p.add_argument("--work",type=Path,required=True);p.add_argument("--budget-seconds",type=float,default=600)
    p.add_argument("--max-private-bytes",type=int,required=True)
    a=p.parse_args();ownership=json.loads(a.ownership.read_text());slots=validate_ownership(ownership)
    stream=ownership["owner_id"]
    head_path=safe_path(a.private_store,"heads/"+stream+".json")
    if not head_path.exists(): raise ValueError("private whole-target bootstrap artifact is required")
    head=json.loads(head_path.read_text());latest=safe_path(a.private_store,"bundles/"+head["bundle_id"])
    manifest=read_manifest(latest,expected_manifest_hash=head["manifest_hash"])
    a.work.mkdir(parents=True,exist_ok=True);raw_root=a.work/"raw"
    engine=make_engine(os.environ["BACKTEST_DATABASE_URL"])
    try:
        incoming=FolderTransport(a.private_store).committed_bundles()
        for path in incoming:
            with transaction(engine) as session:import_bundle(session,path,raw_root=raw_root)
        from backtest_data.remote_updates import plan_remote_jobs
        owned_sources={s['source'] for s in slots}
        core_sources={s['source'] for s in slots if s['source'] in CORE_SOURCES and s.get('handler')!='supplier_adjusted'}
        def jobs(checkpoint):
            return plan_remote_jobs(engine,ownership['manifest_id'],datetime.now(timezone.utc),
                enabled_sources=core_sources,since=catchup_since(checkpoint.get('completed_through',{}),owned_sources=core_sources)) if core_sources else []
        retained=sum(f.stat().st_size for f in a.private_store.rglob("*") if f.is_file())
        if retained>=a.max_private_bytes:raise ValueError("unreceived private state retained; acquisition storage budget deferred")
        from backtest_data.remote.source_handlers import run_owned_handlers
        result=run_cycle(engine,raw_root,ownership=ownership,planned_jobs=jobs,incoming_bundles=[],output_root=a.work/"outgoing",
                         transport_root=a.private_store,stream_id=stream,sequence=head["sequence"]+1,
                         checkpoint=loads(json.dumps(manifest["checkpoint"])),budget_seconds=a.budget_seconds,
                         max_private_bytes=a.max_private_bytes,previous_bundle_id=head['bundle_id'],
                         postprocess=daily_publication_step,postprocess_budget_seconds=min(60,a.budget_seconds/5),
                         source_handlers=run_owned_handlers)
        print(json.dumps(result));return 0
    finally:engine.dispose()


if __name__=="__main__":raise SystemExit(main())
