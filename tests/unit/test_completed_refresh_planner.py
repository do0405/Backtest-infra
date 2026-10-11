from datetime import datetime, timezone
from types import SimpleNamespace as N

from backtest_data.ops import source_cadence as mod


def plan(**kwargs):
    import pytest
    members=[{'security_id':str(i),'market':'US','symbol':'AAPL'} for i in range(3)]
    scope={'output_start':'2016-01-01','output_end':'2026-10-07'}
    jobs=[N(source='alpaca',domain='prices',entity_id=m['security_id'],status='succeeded',
        request={**scope,'manifest_id':'whole'}) for m in members]
    try:
        return mod.prepare_refresh_plan(members,[],jobs,manifest_id='whole',scope=scope,enabled_sources=['alpaca'],
            as_of=datetime(2026,10,13,10,tzinfo=timezone.utc),max_jobs=2,**kwargs)
    except AttributeError:
        pytest.fail('bounded resumable completed source planning is required')


def test_refresh_planning_uses_bounded_batches_and_frozen_date_on_resume():
    first=plan()
    assert first['status']=='planning' and len(first['jobs'])==2 and first['checkpoint']['cursor']==2
    second=plan(previous=first['checkpoint'])
    assert second['status']=='ready' and len(second['jobs'])==1 and second['checkpoint']['cursor']==3
    assert {j['entity_id'] for j in first['jobs']+second['jobs']}=={'0','1','2'}
    assert all(j['request']['output_end']=='2026-10-12' for j in first['jobs']+second['jobs'])


def test_refresh_planning_rejects_changed_manifest_or_cursor_binding():
    import pytest
    first=plan();bad={**first['checkpoint'],'manifest_id':'another'}
    with pytest.raises(ValueError,match='binding'):
        plan(previous=bad)
    bad={**first['checkpoint'],'cursor':999}
    with pytest.raises(ValueError,match='cursor'):
        plan(previous=bad)


def test_completed_refresh_cutoff_reports_unsupported_gaps_without_coverage_claim():
    members=[{'security_id':str(i),'market':'US','symbol':'AAPL'} for i in range(3)]
    scope={'output_start':'2016-01-01','output_end':'2026-10-07'}
    roots=[N(source='alpaca',domain='prices',entity_id=m['security_id'],status='succeeded',
        request={**scope,'manifest_id':'whole'}) for m in members]
    refresh=[N(source='alpaca',domain='prices',entity_id=m['security_id'],status='unsupported',
        request={'manifest_id':'whole','refresh_policy':'catchup_and_rolling14_calendar_days_v1',
            'original_backfill_start':'2016-01-01','original_backfill_end':'2026-10-07','output_end':'2026-10-12'}) for m in members]
    result=mod.prepare_refresh_plan(members,[],roots+refresh,manifest_id='whole',scope=scope,enabled_sources=['alpaca'],
        as_of=datetime(2026,10,13,10,tzinfo=timezone.utc))
    assert result['status']=='up_to_date' and result['proofs']['alpaca']['refresh_terminal_gaps']==3
    assert result['proofs']['alpaca']['latest_refresh_cutoff']=='2026-10-12' and result['coverage_complete'] is False
