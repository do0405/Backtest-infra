from datetime import timedelta

from sqlalchemy import text

from backtest_data.db import utcnow
from backtest_data.ingestion.jobs import acquire_job, plan_job
from backtest_data.ingestion.models import CollectionJob


def _nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


def test_runnable_lane_has_ordered_index_access_without_whole_queue_sort(db_session):
    db_session.execute(text("SET LOCAL enable_seqscan=off"))
    # A tiny fixture may prefer sorting a cheaper source/entity lookup. Ask
    # whether an ordered access path exists; production chooses it by cost.
    db_session.execute(text("SET LOCAL enable_sort=off"))
    plan = db_session.execute(text("""EXPLAIN (FORMAT JSON) SELECT id FROM collection_job
        WHERE source='kis_overseas' AND domain='prices' AND status IN ('pending','retry','running')
          AND (status IN ('pending','retry') OR (status='running' AND lease_until<=now()))
        ORDER BY attempts,priority,id LIMIT 1 FOR UPDATE SKIP LOCKED""")).scalar_one()[0]["Plan"]
    nodes = list(_nodes(plan))
    assert not any(node["Node Type"] == "Sort" for node in nodes)
    assert any("source" in node.get("Index Cond", "") and "domain" in node.get("Index Cond", "")
               for node in nodes)


def test_request_reuse_can_seek_exact_source_domain_entity(db_session):
    db_session.execute(text("SET LOCAL enable_seqscan=off"))
    plan = db_session.execute(text("""EXPLAIN (FORMAT JSON) SELECT id FROM collection_job
        WHERE source='sec' AND domain='issuer_discovery' AND entity_id='lookup_probe'
          AND request-'manifest_id'='{}'::jsonb AND status!='superseded' LIMIT 1""")).scalar_one()[0]["Plan"]
    assert any(all(key in node.get("Index Cond", "") for key in ("source", "domain", "entity_id"))
               for node in _nodes(plan))


def test_acquisition_delay_does_not_consume_the_new_owners_lease(db_session):
    selected_at = utcnow()
    acquired_at = selected_at + timedelta(minutes=6)
    job = plan_job(db_session, source="acquisition_probe", domain="prices", entity_id="late_lock",
                   request={"market": "US"})
    db_session.flush()
    lease = acquire_job(db_session, "new_owner", selected_at, timedelta(minutes=5),
                        sources=["acquisition_probe"], acquisition_clock=lambda: acquired_at)
    db_session.refresh(job)
    assert lease.job_id == job.id
    assert job.lease_until == acquired_at + timedelta(minutes=5)
    assert job.lease_owner == "new_owner" and job.fencing_token == lease.fencing_token


def test_existing_explicit_clock_call_remains_deterministic(db_session):
    selected_at = utcnow()
    job = plan_job(db_session, source="explicit_clock_probe", domain="prices", entity_id="legacy",
                   request={"market": "US"})
    lease = acquire_job(db_session, "old_contract", selected_at, timedelta(seconds=5),
                        sources=["explicit_clock_probe"])
    db_session.refresh(job)
    assert lease.job_id == job.id and job.lease_until == selected_at + timedelta(seconds=5)
