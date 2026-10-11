from sqlalchemy import func, select

from backtest_data.catalog.collection import save_universe
from backtest_data.catalog.universe import UniverseEntry
from backtest_data.ingestion.jobs import consolidate_pending_jobs, plan_job
from backtest_data.ingestion.models import CollectionJob


def test_new_manifest_reuses_unchanged_full_work(db_session):
    entries = [UniverseEntry("KR", "KONEX", "000001", "KR0000000001", "A", "unclassified", {})]
    first = save_universe(db_session, entries, output_start="2016-01-01", output_end="2026-10-07", evidence=[{"source": "fixture", "hash": "f" * 64}], gaps=[])
    assert first.scope["directory_evidence"] == [{"source": "fixture", "hash": "f" * 64}]
    entries.append(UniverseEntry("KR", "KONEX", "000002", "KR0000000002", "B", "unclassified", {}))
    second = save_universe(db_session, entries, output_start="2016-01-01", output_end="2026-10-07", evidence=[], gaps=[])
    assert first.id != second.id and len(second.members) == 2
    assert db_session.scalar(select(func.count()).select_from(CollectionJob)) == 6


def test_manifest_binding_does_not_duplicate_identical_discovery_work(db_session):
    first = plan_job(db_session, source="sec", domain="issuer_discovery", entity_id="issuer", request={"market": "US", "manifest_id": "one", "output_end": "2026-10-07"})
    second = plan_job(db_session, source="sec", domain="issuer_discovery", entity_id="issuer", request={"market": "US", "manifest_id": "two", "output_end": "2026-10-07"})
    assert first.id == second.id


def test_pending_duplicates_are_superseded_without_deleting_target_or_evidence(db_session):
    from backtest_data.db import new_id
    first = CollectionJob(id=new_id(), identity_hash="1" * 64, source="kis", domain="prices", entity_id="security", request={"market": "KR", "manifest_id": "one"}, status="retry", checkpoint={"cursor_end": "2020-01-01"}, attempts=3)
    second = CollectionJob(id=new_id(), identity_hash="2" * 64, source="kis", domain="prices", entity_id="security", request={"market": "KR", "manifest_id": "two"})
    db_session.add_all([first, second])
    db_session.flush()
    assert consolidate_pending_jobs(db_session) == 1
    db_session.refresh(second)
    assert second.status == "superseded" and second.checkpoint["canonical_job_id"] == first.id
    assert first.checkpoint == {"cursor_end": "2020-01-01"}


def test_repair_revalidates_full_traversal_with_fresh_rejection_counts(db_session):
    from backtest_data.ingestion.jobs import request_repair
    job = plan_job(db_session, source="kis", domain="prices", entity_id="repair-security", request={"market": "KR"})
    job.status, job.checkpoint = "unsupported", {"cursor_end": "2017-01-01", "rejected_total": 100, "source_rows_total": 200, "reason": "source_row_validation_gap"}
    db_session.flush()
    assert request_repair(db_session, domains=["prices"], market="kr") == 1
    assert job.status == "retry" and "cursor_end" not in job.checkpoint
    assert job.checkpoint["rejected_total"] == 0
    assert job.checkpoint["source_rows_total"] == 0
    assert job.checkpoint["prior_traversal"]["rejected_total"] == 100


def test_scoped_full_period_repair_preserves_other_work_and_source_evidence(db_session):
    from copy import deepcopy
    from backtest_data.ingestion.jobs import request_repair
    targets = []
    for source, market, cursor in [('kis', 'KR', None), ('kis_overseas', 'US', '2026-05-27')]:
        job = plan_job(db_session, source=source, domain='prices', entity_id=source,
            request={'market':market, 'output_start':'2016-01-01', 'output_end':'2026-10-07', 'mapping_status':'mapped'})
        job.status = 'unsupported'
        job.checkpoint = {'cursor_end':cursor, 'request_anchor':'2026-10-09T00:00:00Z',
            'next_page_token':'old-token', 'rejected_total':7, 'source_rows_total':100,
            'reason':'source_row_validation_gap'}
        targets.append(job)
    excluded = plan_job(db_session, source='kis', domain='prices', entity_id='excluded', request={'market':'KR'})
    unmapped = plan_job(db_session, source='kis_overseas', domain='prices', entity_id='unmapped',
        request={'market':'US','mapping_status':'unsupported'})
    active = plan_job(db_session, source='kis_overseas', domain='prices', entity_id='active', request={'market':'US'})
    excluded.status = unmapped.status = 'unsupported'; active.status = 'running'; active.attempts = 10
    db_session.flush()
    others = [excluded, unmapped, active]
    snapshots = [deepcopy({c.key:getattr(j,c.key) for c in j.__table__.columns}) for j in others]
    original_requests = [deepcopy(j.request) for j in targets]
    assert request_repair(db_session, domains=['prices'], market='both',
        job_ids=[j.id for j in targets] + [unmapped.id,active.id]) == 2
    for job, original in zip(targets, original_requests):
        assert job.status == 'retry' and job.request == original
        assert all(key not in job.checkpoint for key in ('cursor_end','next_page_token','request_anchor'))
        assert job.checkpoint['rejected_total'] == job.checkpoint['source_rows_total'] == 0
        assert job.checkpoint['prior_traversal']['rejected_total'] == 7
    assert snapshots == [{c.key:getattr(j,c.key) for c in j.__table__.columns} for j in others]


def test_empty_explicit_repair_scope_never_requeues_all_jobs(db_session):
    from backtest_data.ingestion.jobs import request_repair
    job = plan_job(db_session, source='kis', domain='prices', entity_id='not-selected', request={'market':'KR'})
    job.status = 'unsupported'; db_session.flush()
    assert request_repair(db_session, domains=['prices'], market='both', job_ids=[]) == 0
    assert job.status == 'unsupported'
