from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from backtest_data.db import utcnow
from backtest_data.ingestion.jobs import plan_job, acquire_job, complete_job, reserve_rate_slot


pytestmark = pytest.mark.integration


def test_expired_worker_cannot_commit_and_pending_work_survives(db_session):
    first = plan_job(db_session, source="sec", domain="fundamentals", entity_id="issuer1", request={"start": "2016"})
    duplicate = plan_job(db_session, source="sec", domain="fundamentals", entity_id="issuer1", request={"start": "2016"})
    assert duplicate.id == first.id
    now = utcnow()
    lease1 = acquire_job(db_session, "worker1", now, timedelta(seconds=1))
    lease2 = acquire_job(db_session, "worker2", now + timedelta(seconds=2), timedelta(seconds=20))
    assert lease1.fencing_token < lease2.fencing_token
    with pytest.raises(ValueError, match="lease"):
        complete_job(db_session, lease1, now + timedelta(seconds=3), {"cursor": "wrong"})
    complete_job(db_session, lease2, now + timedelta(seconds=3), {"cursor": "good"})


def test_workers_share_one_source_rate_budget(db_session):
    now = utcnow()
    first = reserve_rate_slot(db_session, "sec:shared", now, 0.2)
    second = reserve_rate_slot(db_session, "sec:shared", now, 0.2)
    assert (second - first).total_seconds() == pytest.approx(0.2)
