from datetime import date, datetime, timezone

from backtest_data.remote_updates import build_update_jobs


def test_local_daily_refresh_uses_per_source_cutoffs_and_bounded_offline_catchup():
    members=[{'market':'KR','symbol':'005930','security_id':'kr'},{'market':'US','symbol':'AAPL','security_id':'us'}]
    scope={'output_start':'2016-01-01','output_end':'2026-10-07'}
    jobs=build_update_jobs(members,[],manifest_id='full',original_scope=scope,
        as_of=datetime(2026,12,12,13,tzinfo=timezone.utc),enabled_sources=['kis','alpaca'],
        source_since={'kis':date(2026,12,10),'alpaca':date(2026,10,7)},publication_aware=True,max_catchup_days=31)
    kr,us=sorted(jobs,key=lambda j:j['source'],reverse=True)
    assert kr['request']['output_end']=='2026-12-11'
    assert kr['request']['output_start']=='2026-11-28'
    assert us['request']['output_end']=='2026-11-06'
    assert us['request']['output_start']=='2026-10-08'
    assert all(j['request']['original_backfill_end']=='2026-10-07' for j in jobs)


def test_publication_aware_plan_skips_sources_without_new_eligible_date():
    jobs=build_update_jobs([{'market':'US','symbol':'AAPL','security_id':'us'}],[],manifest_id='full',
        original_scope={'output_start':'2016-01-01','output_end':'2026-10-07'},
        as_of=datetime(2026,10,13,9,59,tzinfo=timezone.utc),enabled_sources=['alpaca'],
        source_since={'alpaca':date(2026,10,9)},publication_aware=True,max_catchup_days=31)
    assert jobs==[]
