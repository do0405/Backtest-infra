"""Owned, bounded SQL-only postprocessing before private delta publication."""
import json
from pathlib import Path
from .codec import dumps


def daily_publication_step(engine, *, manifest_id, owned_sources, checkpoint,
                           budget_seconds, work_root):
    if not set(owned_sources)&{'sec','opendart'} or budget_seconds<=0:return {}
    from ..fundamentals.daily_publication_runner import backfill_daily_publications
    path=Path(work_root)/'daily-publication-progress.json';path.parent.mkdir(parents=True,exist_ok=True)
    previous=checkpoint.get('daily_publication_progress')
    if previous and previous.get('status')!='source_pass_complete':
        dumps(previous)
        path.write_text(json.dumps(previous,ensure_ascii=False),encoding='utf-8')
    elif path.exists():path.unlink()
    summary=backfill_daily_publications(engine,manifest_id=manifest_id,progress_path=path,
        budget_seconds=budget_seconds,batch_size=25,max_documents=100)
    progress=json.loads(path.read_text(encoding='utf-8'));dumps(progress)
    # Local filesystem paths and ephemeral runtime state are not checkpointed.
    return {'daily_publication_progress':progress,'daily_publication_status':summary['status'],'coverage_complete':False}
