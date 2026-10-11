from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


def test_interrupted_150_rows_restore_next_page_and_original_capture_anchor():
    from backtest_data.remote import checkpoint as module
    from backtest_data.remote.codec import dumps, loads
    anchor = '2026-10-10T05:00:00+00:00'
    job = SimpleNamespace(id='remote-job', identity_hash='stable-work', source='alpaca', domain='prices', entity_id='original-security',
        request={'market':'US','symbol':'AAPL','output_start':'2016-01-01','output_end':'2026-10-07'}, priority=10,
        status='retry', attempts=1, checkpoint={'next_page_token':'page-100','page_size':100,'request_anchor':anchor,
            'source_rows_total':100,'rejected_total':0,'lease_owner':'must-not-transfer'})
    state = module.snapshot_jobs([job], owned_sources={'alpaca'})
    restored = module.runtime_task(loads(dumps(state))[0])
    from datetime import date
    from backtest_data.prices.adapters.alpaca import AlpacaDailyRequest
    request=AlpacaDailyRequest(date(2016,1,1),date(2026,10,7),symbol='AAPL',
        page_token=restored['checkpoint']['next_page_token'],page_size=restored['checkpoint']['page_size'],
        captured_at=datetime.fromisoformat(restored['checkpoint']['request_anchor']))
    calls = []
    def page(cursor):
        calls.append(cursor)
        return list(range(100,150)) if cursor == 'page-100' else list(range(100))
    next_rows = page(request.params()['page_token'])
    assert calls == ['page-100']
    assert next_rows == list(range(100,150))
    assert restored['checkpoint']['request_anchor'] == anchor
    assert restored['checkpoint']['source_rows_total'] == 100
    assert 'lease_owner' not in restored['checkpoint']
    assert len(list(range(100)) + next_rows) == 150


def test_discovered_child_jobs_and_succeeded_receipts_survive_checkpoint():
    from backtest_data.remote.checkpoint import snapshot_jobs
    parent = SimpleNamespace(id='parent',identity_hash='parent-work',source='sec',domain='issuer_discovery',entity_id='issuer-id',request={'market':'US'},priority=10,status='succeeded',attempts=1,checkpoint={})
    child = SimpleNamespace(id='child',identity_hash='child-work',source='sec',domain='filing_sql',entity_id='new-filing-id',request={'market':'US','filing_id':'new-filing-id'},priority=5,status='pending',attempts=0,checkpoint={})
    state = snapshot_jobs([parent,child],owned_sources={'sec'})
    assert [(j['id'],j['status']) for j in state] == [('parent','succeeded'),('child','pending')]


def test_page_cursor_is_typed_bounded_and_auth_values_remain_forbidden():
    from backtest_data.remote.codec import PaginationCursor,dumps,loads
    assert loads(dumps({'next_page_token':PaginationCursor('page-two')}))['next_page_token'].value == 'page-two'
    with pytest.raises(ValueError,match='private'):dumps({'next_page_token':'untyped'})
    with pytest.raises(ValueError):PaginationCursor('x'*4097)
    with pytest.raises(ValueError,match='private'):dumps({'Authorization':PaginationCursor('opaque')})


def test_original_bar_page_evidence_keeps_only_recognized_bounded_pagination_exception(tmp_path):
    import gzip, hashlib, json
    from backtest_data.remote.sql import _verify_raw
    source=json.dumps({'bars':[{'c':'1.23'}],'next_page_token':'page-100'}).encode()
    path=tmp_path/'source.gz';path.write_bytes(gzip.compress(source))
    _verify_raw(path,{'byte_count':len(source),'content_hash':hashlib.sha256(source).hexdigest()})
    assert gzip.decompress(path.read_bytes())==source
    source=json.dumps({'bars':[],'access_token':'not-pagination'}).encode();path.write_bytes(gzip.compress(source))
    with pytest.raises(ValueError,match='private'):_verify_raw(path,{'byte_count':len(source),'content_hash':hashlib.sha256(source).hexdigest()})


def test_refresh_watermark_keeps_gaps_without_pinning_future_planning():
    from backtest_data.remote.checkpoint import refresh_watermarks, catchup_since
    completed = SimpleNamespace(source='alpaca',status='succeeded',request={'output_end':'2026-10-09'})
    unfinished = SimpleNamespace(source='sec',status='pending',request={'filing_id':'child'})
    gap = SimpleNamespace(source='sec',status='unsupported',request={'output_end':'2026-10-09'})
    result = refresh_watermarks([completed,unfinished,gap], {'sec':'2026-10-07'}, owned_sources={'alpaca','sec'})
    assert result == {'alpaca':'2026-10-09','sec':'2026-10-07'}
    assert catchup_since(result, owned_sources={'alpaca','sec'}).isoformat() == '2026-10-07'
    # A newer refresh is still planned by the caller at its current as_of date.
    assert catchup_since(result, owned_sources={'alpaca'}).isoformat() == '2026-10-09'


def test_checkpoint_safe_pagination_roundtrips_through_bundle_manifest(tmp_path):
    import json
    from backtest_data.remote.bundle import write_bundle, read_manifest
    from backtest_data.remote.codec import PaginationCursor, loads
    scope={'manifest_id':'original-manifest','target_count':17758,'output_start':'2016-01-01','output_end':'2026-10-07'}
    path=write_bundle(tmp_path,[],stream_id='private-remote',sequence=2,scope=scope,
        checkpoint={'task_state':[{'checkpoint':{'next_page_token':PaginationCursor('page-100')}}]})
    decoded=loads(json.dumps(read_manifest(path)['checkpoint']))
    assert decoded['task_state'][0]['checkpoint']['next_page_token'].value=='page-100'


def test_latest_successful_window_can_advance_past_preserved_old_receipts():
    from backtest_data.remote.checkpoint import refresh_watermarks
    jobs=[SimpleNamespace(source='alpaca',entity_id='security-one',domain='prices',status='succeeded',request={'output_end':end})
          for end in ('2026-10-07','2026-10-09')]
    jobs.append(SimpleNamespace(source='alpaca',entity_id='security-two',domain='prices',status='succeeded',request={'output_end':'2026-10-08'}))
    assert refresh_watermarks(jobs,{},owned_sources={'alpaca'})=={'alpaca':'2026-10-08'}


def test_task_restore_never_overwrites_live_queue_or_copies_lease_authority():
    from backtest_data.remote.checkpoint import restore_tasks
    class Queue:
        def __init__(self,existing=None):self.existing=existing;self.jobs=[]
        def scalar(self,_):return self.existing
        def add_all(self,jobs):self.jobs.extend(jobs)
        def flush(self):pass
    state={'id':'portable-child','identity_hash':'a'*64,'source':'sec','domain':'filing_sql','entity_id':'filing-id',
           'request':{'market':'US','filing_id':'filing-id'},'priority':5,'status':'pending','attempts':0,'checkpoint':{}}
    with pytest.raises(ValueError,match='authority'):restore_tasks(Queue(),[state],owned_sources={'sec'},authority='local')
    live=Queue(existing='local-job')
    with pytest.raises(ValueError,match='empty'):restore_tasks(live,[state],owned_sources={'sec'},authority='new_remote_ephemeral_database')
    assert live.jobs==[]
    fresh=Queue();assert restore_tasks(fresh,[state],owned_sources={'sec'},authority='new_remote_ephemeral_database')==1
    assert fresh.jobs[0].id=='portable-child'
    assert fresh.jobs[0].lease_owner is None and fresh.jobs[0].lease_until is None and fresh.jobs[0].fencing_token==0


def test_daily_postprocess_restores_private_progress_and_only_financial_owners(tmp_path,monkeypatch):
    import json
    from backtest_data.remote.postprocess import daily_publication_step
    from backtest_data.fundamentals import daily_publication_runner
    calls=[]
    def backfill(engine,**kwargs):
        path=kwargs['progress_path'];calls.append((json.loads(path.read_text()),kwargs))
        path.write_text(json.dumps({'status':'budget_deferred','cursor':'second-document','coverage_complete':False}))
        return {'status':'budget_deferred','coverage_complete':False}
    monkeypatch.setattr(daily_publication_runner,'backfill_daily_publications',backfill)
    checkpoint={'daily_publication_progress':{'status':'budget_deferred','cursor':'first-document','coverage_complete':False}}
    assert daily_publication_step(object(),manifest_id='full-manifest',owned_sources={'alpaca'},checkpoint=checkpoint,
        budget_seconds=5,work_root=tmp_path)=={}
    result=daily_publication_step(object(),manifest_id='full-manifest',owned_sources={'sec'},checkpoint=checkpoint,
        budget_seconds=5,work_root=tmp_path)
    assert calls[0][0]['cursor']=='first-document'
    assert calls[0][1]['manifest_id']=='full-manifest'
    assert result['daily_publication_progress']['cursor']=='second-document'
    assert result['coverage_complete'] is False


def test_derived_lineage_closure_rejects_unknown_domains_and_unbound_calendar():
    from backtest_data.remote.registry import logical_parents
    row={'table':'derived_fact','values':{'entity_type':'issuer','entity_id':'original-issuer',
         'input_refs':[{'domain':'fundamental_fact','id':'canonical-input'}, {'domain':'financial_daily_publication_evidence','id':'proof'}]}}
    assert set(logical_parents(row))=={('issuer','original-issuer'),('fundamental_fact','canonical-input'),('financial_daily_publication_evidence','proof')}
    row['values']['input_refs']=[{'domain':'token_state','id':'credential'}]
    with pytest.raises(ValueError,match='lineage'):logical_parents(row)
    with pytest.raises(ValueError,match='lineage'):logical_parents({'table':'selection_decision','values':{'record_domain':'collection_job','record_id':'operational'}})
