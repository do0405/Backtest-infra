"""Real unchanged-row lock waits against the separate PostgreSQL test database."""
from datetime import timedelta
import threading
import time

import pytest
from sqlalchemy import delete, event, func, insert, select, update
from sqlalchemy.orm import Session

from backtest_data.catalog.models import CatalogObservation
from backtest_data.db import new_id
from backtest_data.ingestion import jobs
from backtest_data.ingestion.models import CollectionJob


pytestmark = pytest.mark.integration
TEST_TTL = timedelta(seconds=10)
SOURCE = "live_lease_test_probe"


def call_current(session, lease, sampled_at, operation, *, ttl=timedelta(seconds=30)):
    if operation == "heartbeat":
        return getattr(jobs, "heartbeat_current_job", jobs.heartbeat_job)(session, lease, sampled_at, ttl)
    return getattr(jobs, "complete_current_job", jobs.complete_job)(session, lease, sampled_at, {"cursor": "changed"})


@pytest.mark.parametrize("operation", ["heartbeat", "complete"])
def test_unchanged_row_lock_wait_past_expiry_cannot_revive_or_complete(pg_engine, operation):
    identity = new_id()
    with pg_engine.begin() as connection:
        # Remove only mutable probes from an earlier red run of this file;
        # immutable audit evidence is retained under its existing SQL guard.
        connection.execute(delete(CollectionJob).where(CollectionJob.source == SOURCE,
            CollectionJob.domain == "lease_clock_test"))
        connection.execute(insert(CollectionJob).values(id=identity, identity_hash=jobs.stable_hash(identity),
            source=SOURCE, domain="lease_clock_test", entity_id=identity, request={}, status="running",
            checkpoint={"cursor": "original"}, lease_owner="owner", fencing_token=7,
            lease_until=None))
    deadline, sampled_at = [None], [None]
    lease = jobs.JobLease(identity, "owner", 7)
    prepared, resume, started, finished = (threading.Event() for _ in range(4))
    failures, worker_pids = [], []

    def worker():
        try:
            with Session(pg_engine) as session, session.begin():
                connection = session.connection()
                worker_pids.append(connection.scalar(select(func.pg_backend_pid())))

                def entering(connection, cursor, statement, parameters, context, executemany):
                    if (not started.is_set() and "collection_job" in statement
                            and ("FOR UPDATE" in statement or statement.startswith("UPDATE"))):
                        fresh = connection.scalar(select(func.clock_timestamp()))
                        assert deadline[0] > fresh, "short test lease expired before the row-lock wait began"
                        started.set()

                event.listen(connection, "before_cursor_execute", entering)
                try:
                    session.execute(insert(CatalogObservation).values(id=new_id(), source=SOURCE, identifier=identity,
                        content_hash=jobs.stable_hash([identity, "page"]), fields={"page": "must roll back"}))
                    prepared.set()
                    assert resume.wait(30), "test did not resume prepared page"
                    call_current(session, lease, sampled_at[0], operation)
                    # A red-run helper may wrongly return success. Keep the
                    # synthetic page in rollback while reporting that failure.
                    raise AssertionError("expired operation incorrectly returned success")
                finally:
                    event.remove(connection, "before_cursor_execute", entering)
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=worker, daemon=True)
    try:
        thread.start()
        assert prepared.wait(30), "worker did not prepare the synthetic page"
        with pg_engine.begin() as connection:
            deadline[0] = connection.execute(update(CollectionJob).where(CollectionJob.id == identity)
                .values(lease_until=func.clock_timestamp() + TEST_TTL)
                .returning(CollectionJob.lease_until)).scalar_one()
        with pg_engine.connect() as blocker:
            blocking_tx = blocker.begin()
            blocker_pid = blocker.scalar(select(func.pg_backend_pid()))
            blocker.execute(select(CollectionJob.id).where(CollectionJob.id == identity).with_for_update()).scalar_one()
            sampled_at[0] = blocker.scalar(select(func.clock_timestamp()))
            assert deadline[0] > sampled_at[0], "test seed commit consumed the short lease"
            resume.set()
            assert started.wait(30), "test worker did not reach the guarded job statement"
            wait_deadline = time.monotonic() + 30
            while True:
                # Inspect only the worker backend created by this test.
                blockers = blocker.scalar(select(func.pg_blocking_pids(worker_pids[0])))
                if blocker_pid in blockers:
                    break
                assert not finished.is_set() and time.monotonic() < wait_deadline, "worker did not wait on unchanged test row"
                time.sleep(0.02)
            fresh = blocker.scalar(select(func.clock_timestamp()))
            remaining = (deadline[0] - fresh).total_seconds()
            if remaining > 0:
                time.sleep(remaining)
            time.sleep(0.05)
            # Release the row without updating its tuple or expiry timestamp.
            blocking_tx.commit()
        assert finished.wait(30), "test worker did not finish after row lock release"
        thread.join(timeout=1)
        assert len(failures) == 1 and isinstance(failures[0], jobs.LeaseLost), "expired live operation succeeded after unchanged row-lock wait"
        with Session(pg_engine) as session:
            job = session.get(CollectionJob, identity)
            assert job.status == "running" and job.checkpoint == {"cursor": "original"}
            assert job.lease_until == deadline[0] and job.lease_owner == "owner" and job.fencing_token == 7
            assert not session.scalars(select(CatalogObservation.id).where(
                CatalogObservation.source == SOURCE, CatalogObservation.identifier == identity)).all()
    finally:
        resume.set()
        if thread.is_alive():
            finished.wait(30)
            thread.join(timeout=1)
        with pg_engine.begin() as connection:
            connection.execute(delete(CollectionJob).where(CollectionJob.source == SOURCE, CollectionJob.id == identity))
