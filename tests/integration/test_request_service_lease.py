from datetime import timedelta

from backtest_data.db import utcnow
from backtest_data.ingestion.jobs import acquire_job,plan_job


def test_lease_service_filter_never_claims_other_service(db_session):
    wrong=plan_job(db_session,source='krx',domain='market_quotes',entity_id='kospi:2020-01-02',
        request={'market':'KR','service':'kospi','session_date':'2020-01-02'},priority=0)
    selected=plan_job(db_session,source='krx',domain='market_quotes',entity_id='etf:2020-01-02',
        request={'market':'KR','service':'etf','session_date':'2020-01-02'})
    lease=acquire_job(db_session,'owner',utcnow(),timedelta(minutes=5),
        sources=['krx'],domains=['market_quotes'],request_services=['etf'])
    assert lease.job_id==selected.id and wrong.status=='pending' and wrong.attempts==0
    assert lease.owner=='owner' and lease.fencing_token==1
    assert acquire_job(db_session,'other',utcnow(),timedelta(minutes=5),
        sources=['krx'],domains=['market_quotes'],request_services=[]) is None
