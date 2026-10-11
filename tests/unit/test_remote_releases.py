from datetime import datetime,timezone
from pathlib import Path
import json
from unittest.mock import patch
import pytest
from backtest_data.remote import releases as r


def test_checkpoint_exact_clock_and_typed_cursor_roundtrip(tmp_path):
    from backtest_data.remote.codec import PaginationCursor
    value={'not_before':datetime(2026,10,11,5,0,0,123456,tzinfo=timezone.utc),
           'checkpoint':{'next_page_token':PaginationCursor('opaque-page')}}
    path=tmp_path/'state.json.gz';r.write_state(path,value)
    assert r.read_state(path)==value
    with pytest.raises(ValueError,match='private'):
        r.write_state(tmp_path/'bad.gz',{'API_KEY':'credential'})


def test_commit_stream_and_catalog_binding_are_required(tmp_path):
    value={'format':'backtest_release_commit_v1','stream_id':'krx-live-test','start_sequence':1,
        'end_sequence':1,'previous_bundle_id':None,'head':{'bundle_id':'3bcbb5a4-677f-4f75-a8a5-dd29fd587390','sequence':1,'manifest_hash':'b'*64},'previous_tag':None,'previous_commit_sha256':None,
        'bootstrap_sha256':'a'*64,'files':{name:{'bytes':1,'sha256':'b'*64} for name in ('delta.zip','state.json.gz')},
        'cloud_run_id':'1234','created_at':'2026-10-11T05:00:00+00:00','counts':{},'source_status':[],'coverage_complete':False}
    path=tmp_path/'commit.json';path.write_text(json.dumps(value))
    assert r.read_commit(path,stream='krx-live-test',bootstrap_hash='a'*64,tag='data-krx-live-test-000000001')==value
    for keywords in ({'stream':'different','bootstrap_hash':'a'*64},{'stream':'krx-live-test','bootstrap_hash':'c'*64}):
        with pytest.raises(ValueError):r.read_commit(path,**keywords)
    import copy
    for changes in ({'head':{}},{'previous_tag':'unbound'},{'cloud_run_id':'bad'},
                    {'counts':{'unregistered_table':1}}, {'created_at':'2026-10-11T05:00:00'},
                    {'source_status':[{'handler':'krx_quotes','status':'blocked','reason':'authentication_required','requires_user':'yes'}]}):
        changed=copy.deepcopy(value);changed.update(changes);path.write_text(json.dumps(changed))
        with pytest.raises(ValueError):r.read_commit(path,stream='krx-live-test',bootstrap_hash='a'*64)


def test_provider_authentication_is_actionable_but_quota_is_not():
    assert r.source_status([{'handler':'krx_quotes','status':'blocked','blocked_sources':{'krx':'401'}}])[0]['requires_user']
    assert not r.source_status([{'handler':'krx_quotes','status':'blocked','blocked_sources':{'krx':'429'}}])[0]['requires_user']
    assert r.source_status([{'handler':'krx_quotes','status':'source_error','reason':'ProgrammingError'}])[0]['requires_user']
    assert not r.source_status([{'handler':'krx_quotes','status':'partial','collection':{'blocked_sources':{'krx':'lease_lost'}}}])[0]['requires_user']
    assert not r.source_status([{'handler':'krx_quotes','status':'source_error','reason':'OperationalError','retryable':True}])[0]['requires_user']


def test_inventory_excludes_unpublished_drafts_and_unrelated_releases():
    values=[[{'tag_name':'data-krx-test-000000001','draft':False,'prerelease':False},
        {'tag_name':'data-krx-test-000000002','draft':True,'prerelease':False},
        {'tag_name':'version-3','draft':False,'prerelease':False}]]
    with patch.object(r,'github',return_value=json.dumps(values).encode()):
        assert [v['tag_name'] for v in r.inventory('gh','a/b','krx-test')]==['data-krx-test-000000001']


def test_import_group_checks_final_head_before_any_sql_write(tmp_path):
    from backtest_data.remote.bundle import write_bundle,read_manifest,digest
    store=tmp_path/'store';scope={'target_count':17758,'output_start':'2016-01-01','output_end':'2026-10-07'}
    bundle=write_bundle(store/'bundles',[],stream_id='krx-test',sequence=1,scope=scope)
    m=read_manifest(bundle)
    commit={'start_sequence':1,'end_sequence':1,'previous_bundle_id':None,'stream_id':'krx-test',
        'head':{'bundle_id':m['bundle_id'],'sequence':1,'manifest_hash':'c'*64}}
    with patch.object(r,'transaction') as tx:
        with pytest.raises(ValueError,match='final checkpoint hash'):r.import_release(None,store,commit,tmp_path/'raw')
        tx.assert_not_called()


def test_singleton_sync_lock_prevents_second_daemon(tmp_path):
    import importlib.util
    source=Path(__file__).resolve().parents[2]/'scripts/local_sync_daemon.py'
    spec=importlib.util.spec_from_file_location('sync_lock_test',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    lock=module.lock_file(tmp_path/'sync.lock')
    try:
        with pytest.raises(RuntimeError,match='duplicate'):module.lock_file(tmp_path/'sync.lock')
    finally:lock.close()
