from datetime import date,datetime,timezone
from backtest_data.remote_updates import build_update_jobs,update_window

def test_all_target_members_and_issuer_corrections_remain_in_refresh_plan():
    members=[{'market':'KR','symbol':'005930','security_id':'kr'},{'market':'US','symbol':'AAPL','security_id':'us'}]
    scope={'output_start':'2016-01-01','output_end':'2026-10-07'}
    jobs=build_update_jobs(members,[{'id':'issuer','market':'US'}],manifest_id='full',original_scope=scope,
        as_of=datetime(2026,10,10,5,tzinfo=timezone.utc))
    assert len(jobs)==4
    assert {j['request']['target_count'] for j in jobs}=={2}
    assert {j['entity_id'] for j in jobs if j['domain']=='prices'}=={'kr','us'}
    assert all(j['request']['output_end']=='2026-10-09' for j in jobs)
    assert next(j for j in jobs if j['domain']=='issuer_discovery')['request']['discovery_start']<'2026-09-01'

def test_offline_gap_is_not_lost_to_recent_window_or_future_day():
    start,end=update_window(datetime(2026,10,10,5,tzinfo=timezone.utc),'US',since=date(2026,1,1))
    assert start==date(2026,1,2) and end==date(2026,10,9)

def test_unowned_sources_are_not_planned():
    jobs=build_update_jobs([{'market':'US','symbol':'AAPL','security_id':'us'}],[],manifest_id='full',
        original_scope={'output_start':'2016-01-01','output_end':'2026-10-07'},as_of=datetime(2026,10,10,5,tzinfo=timezone.utc),enabled_sources=['alpaca'])
    assert len(jobs)==1 and jobs[0]['source']=='alpaca'

def test_master_restore_requires_exact_qualified_mapping_no_default_true():
    from backtest_data.remote_updates import restored_mapping
    member={'symbol':'AAPL','security_id':'US-ID'}
    fields={'provider_symbol':'AAPL','exchange':'NAS','master_realtime_symbol':'NASAAPL','realtime_symbol':'DNASAAPL',
        'master_hash':'a'*64,'master_raw_id':'raw','master_captured_at':'2026-10-10T00:00:00Z'}
    assert restored_mapping(fields,member,'catalog') is None
    fields['qualified_current_mapping']=True
    proof=restored_mapping(fields,member,'catalog')
    assert proof['security_id']=='US-ID' and proof['source']=='kis_overseas_master'
    assert restored_mapping({**fields,'provider_symbol':'MSFT'},member,'catalog') is None
