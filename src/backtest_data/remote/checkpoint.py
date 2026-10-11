"""Portable owned-job state; authority reset is restricted to ephemeral SQL."""
from datetime import date
from sqlalchemy import select
from ..ingestion.models import CollectionJob
from .codec import PaginationCursor, dumps

SAFE_CHECKPOINT = frozenset({'cursor_end','page_size','request_anchor','anchor_asof_date','output_start','output_end',
    'report_date','deferred_recent_start','source_rows_total','source_row_count','rejected_total','rejected','inserted',
    'response_valid','pagination_valid','source_format_complete','economic_coverage_verified','reason','retry_after',
    'last_completed_date','last_completed_page','remaining_start'})
STATUSES = {'pending','retry','succeeded','unsupported','failed','superseded'}


def snapshot_jobs(jobs, *, owned_sources):
    result=[]
    for job in jobs:
        if job.source not in owned_sources:continue
        checkpoint={k:v for k,v in job.checkpoint.items() if k in SAFE_CHECKPOINT}
        if job.checkpoint.get('next_page_token'):
            checkpoint['next_page_token']=PaginationCursor(job.checkpoint['next_page_token'])
        state={'id':job.id,'identity_hash':job.identity_hash,'source':job.source,'domain':job.domain,'entity_id':job.entity_id,
            'request':dict(job.request),'priority':job.priority,'status':'retry' if job.status=='running' else job.status,
            'attempts':job.attempts,'checkpoint':checkpoint}
        dumps(state);result.append(state)
    return result


def runtime_task(state):
    required={'id','identity_hash','source','domain','entity_id','request','priority','status','attempts','checkpoint'}
    if set(state)!=required or state['status'] not in STATUSES:raise ValueError('invalid portable task state')
    checkpoint={k:v for k,v in state['checkpoint'].items() if k in SAFE_CHECKPOINT}
    cursor=state['checkpoint'].get('next_page_token')
    if cursor is not None:
        if not isinstance(cursor,PaginationCursor):raise ValueError('typed pagination cursor required')
        checkpoint['next_page_token']=cursor.value
    if set(state['checkpoint'])-SAFE_CHECKPOINT-{'next_page_token'}:raise ValueError('nonportable task checkpoint field')
    return {**state,'checkpoint':checkpoint}


def restore_tasks(session, state, *, owned_sources, authority):
    if authority!='new_remote_ephemeral_database':raise ValueError('explicit new ephemeral task authority required')
    # A live/local queue is never overwritten. Workflow restores into a fresh PG.
    if session.scalar(select(CollectionJob.id).limit(1)) is not None:raise ValueError('ephemeral task restore requires an empty operational queue')
    jobs=[]
    for portable in state:
        dumps(portable)
        if portable['source'] not in owned_sources:raise ValueError('task outside explicitly owned source')
        values=runtime_task(portable)
        jobs.append(CollectionJob(**values,lease_owner=None,lease_until=None,fencing_token=0))
    session.add_all(jobs);session.flush()
    return len(jobs)


def refresh_watermarks(jobs, previous, *, owned_sources):
    """Conservative scheduling watermarks, never a historical coverage claim.

    Gaps prevent advancement for their source; they do not prevent planning a
    new rolling window or restoring existing pagination/child work next cycle.
    """
    result={source:value for source,value in previous.items() if source in owned_sources}
    for source in owned_sources:
        owned=[job for job in jobs if job.source==source]
        if not owned or any(job.status not in {'succeeded','superseded'} for job in owned):continue
        latest={}
        for position,job in enumerate(owned):
            if job.status!='succeeded' or not job.request.get('output_end'):continue
            key=(getattr(job,'entity_id',position),getattr(job,'domain',''))
            end=date.fromisoformat(job.request['output_end'])
            latest[key]=max(end,latest.get(key,end))
        ends=list(latest.values())
        if ends:
            prior=date.fromisoformat(result.get(source,'2026-10-07'))
            result[source]=max(prior,min(ends)).isoformat()
    return result


def catchup_since(watermarks, *, owned_sources):
    """The shared planner receives the least advanced owned-source watermark."""
    if not owned_sources:raise ValueError('explicit owned sources required')
    return min(date.fromisoformat(watermarks.get(source,'2026-10-07')) for source in owned_sources)
