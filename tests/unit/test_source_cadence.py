from datetime import date, datetime, timedelta, timezone
import importlib
from types import SimpleNamespace as N

import pytest


def cadence():
    try:
        return importlib.import_module('backtest_data.ops.source_cadence')
    except ModuleNotFoundError:
        pytest.fail('publication-aware completed source cadence is required')


@pytest.mark.parametrize('source,before,after,expected_before,expected_after', [
    ('krx', '2026-10-11T23:59:59+00:00', '2026-10-12T00:00:00+00:00', date(2026,10,10), date(2026,10,11)),
    ('kis', '2026-10-12T23:59:59+00:00', '2026-10-13T00:00:00+00:00', date(2026,10,9), date(2026,10,12)),
    ('alpaca', '2026-10-13T09:59:59+00:00', '2026-10-13T10:00:00+00:00', date(2026,10,9), date(2026,10,12)),
    ('sec', '2026-11-03T10:59:59+00:00', '2026-11-03T11:00:00+00:00', date(2026,11,1), date(2026,11,2)),
    ('opendart', '2026-10-11T23:59:59+00:00', '2026-10-12T00:00:00+00:00', date(2026,10,10), date(2026,10,11)),
])
def test_ready_dates_respect_project_publication_gate_market_clock_and_dst(source,before,after,expected_before,expected_after):
    mod=cadence()
    assert mod.provider_ready_date(source, datetime.fromisoformat(before))==expected_before
    assert mod.provider_ready_date(source, datetime.fromisoformat(after))==expected_after


def test_ready_date_rejects_naive_or_unregistered_source():
    mod=cadence()
    with pytest.raises(ValueError,match='aware'):
        mod.provider_ready_date('krx',datetime(2026,10,12))
    with pytest.raises(ValueError,match='source'):
        mod.provider_ready_date('data_go',datetime(2026,10,12,tzinfo=timezone.utc))


def original_jobs(scope, services):
    start=date(2016,1,1)
    return [N(source='krx',domain='market_quotes',status='succeeded',
        request={'manifest_id':scope['manifest_id'],'service':service,'session_date':(start+timedelta(days=offset)).isoformat()})
        for offset in range(3933) for service in services]


def test_krx_completion_requires_full_original_scoped_unique_denominator():
    from backtest_data.prices.krx_quote_storage import KRX_SERVICES
    scope={'manifest_id':'whole','output_start':'2016-01-01','output_end':'2026-10-07'}
    mod=cadence();jobs=original_jobs(scope,KRX_SERVICES)
    assert mod.krx_original_complete(jobs,scope,planned_through='2026-10-07')
    assert not mod.krx_original_complete(jobs,scope,planned_through='2026-10-06')
    assert not mod.krx_original_complete([],scope,planned_through='2026-10-07')
    assert not mod.krx_original_complete(jobs[:-1],scope,planned_through='2026-10-07')
    assert not mod.krx_original_complete(jobs[:-1]+[jobs[0]],scope,planned_through='2026-10-07')
    jobs[-1].status='retry'
    assert not mod.krx_original_complete(jobs,scope,planned_through='2026-10-07')
    jobs[-1].status='succeeded';jobs[-1].request['refresh_anchor']='2026-10-12'
    assert not mod.krx_original_complete(jobs,scope,planned_through='2026-10-07')
    jobs[-1].request.pop('refresh_anchor');jobs[-1].request['manifest_id']='another'
    assert not mod.krx_original_complete(jobs,scope,planned_through='2026-10-07')


def test_local_original_traversal_requires_every_root_and_reports_terminal_gaps():
    mod=cadence();members=[{'security_id':'a','market':'KR'},{'security_id':'b','market':'KR'}]
    scope={'output_start':'2016-01-01','output_end':'2026-10-07'}
    jobs=[N(source='kis',domain='prices',entity_id=m['security_id'],status='succeeded',
        request={**scope,'manifest_id':'whole'}) for m in members]
    assert mod.original_traversal(jobs,members,[],source='kis',manifest_id='whole',scope=scope)=={
        'request_pass_complete':True,'terminal_gaps':0,'expected_roots':2,'coverage_complete':False}
    jobs[-1].status='unsupported'
    assert mod.original_traversal(jobs,members,[],source='kis',manifest_id='whole',scope=scope)['terminal_gaps']==1
    for status in ('pending','retry','running','failed','unknown'):
        jobs[-1].status=status
        assert not mod.original_traversal(jobs,members,[],source='kis',manifest_id='whole',scope=scope)['request_pass_complete']
    assert not mod.original_traversal(jobs[:1],members,[],source='kis',manifest_id='whole',scope=scope)['request_pass_complete']


def test_completed_cutoff_requires_whole_successful_refresh_denominator():
    mod=cadence();members=[{'security_id':'a','market':'US'},{'security_id':'b','market':'US'}]
    scope={'output_start':'2016-01-01','output_end':'2026-10-07'}
    jobs=[N(source='alpaca',domain='prices',entity_id=m['security_id'],status='succeeded',
        request={'manifest_id':'whole','refresh_policy':'catchup_and_rolling14_calendar_days_v1',
            'original_backfill_start':'2016-01-01','original_backfill_end':'2026-10-07','output_end':'2026-10-12'}) for m in members]
    kwargs=dict(source='alpaca',manifest_id='whole',scope=scope)
    assert mod.completed_refresh_cutoff(jobs,members,[],**kwargs)==date(2026,10,12)
    jobs[-1].status='retry'
    assert mod.completed_refresh_cutoff(jobs,members,[],**kwargs)==date(2026,10,7)
    assert mod.completed_refresh_cutoff(jobs[:1],members,[],**kwargs)==date(2026,10,7)


def test_filing_original_proof_requires_security_roots_and_every_mapped_issuer():
    mod=cadence();members=[{'security_id':'a','market':'US'},{'security_id':'b','market':'US'}]
    issuers=[{'id':'issuer','market':'US'}];scope={'output_start':'2016-01-01','output_end':'2026-10-07'}
    jobs=[N(source='sec',domain='issuer_discovery',entity_id='issuer',status='succeeded',
        request={**scope,'manifest_id':'whole'})]
    kwargs=dict(source='sec',manifest_id='whole',scope=scope)
    assert not mod.original_traversal(jobs,members,issuers,**kwargs)['request_pass_complete']
    jobs += [N(source='sec',domain='fundamentals',entity_id=m['security_id'],status='unsupported',
        request={**scope,'manifest_id':'whole'}) for m in members]
    proof=mod.original_traversal(jobs,members,issuers,**kwargs)
    assert proof['request_pass_complete'] and proof['expected_roots']==3 and proof['terminal_gaps']==2
