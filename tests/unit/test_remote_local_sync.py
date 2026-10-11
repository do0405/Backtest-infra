from contextlib import contextmanager
import json
import pytest
from backtest_data.remote.bundle import write_bundle,read_manifest
from backtest_data.remote.folder import FolderTransport
from backtest_data.remote import local_sync

SCOPE={'manifest_id':'full','target_count':17758,'output_start':'2016-01-01','output_end':'2026-10-07'}

def test_local_sync_only_imports_head_reachable_bundles_in_sequence(tmp_path,monkeypatch):
    store=tmp_path/'private';transport=FolderTransport(store)
    one=write_bundle(tmp_path/'out',[],stream_id='source',sequence=1,scope=SCOPE)
    head=transport.publish(one)
    two=write_bundle(tmp_path/'out',[],stream_id='source',sequence=2,scope=SCOPE,previous_bundle_id=head['bundle_id'])
    transport.publish(two)
    # An unpublished higher sequence with an otherwise valid manifest stays out.
    orphan=write_bundle(store/'bundles',[],stream_id='source',sequence=3,scope=SCOPE,previous_bundle_id=read_manifest(two)['bundle_id'])
    (orphan/'manifest.json').write_text('malformed unpublished manifest')
    imported=[]
    @contextmanager
    def tx(_):yield object()
    def receive(session,path,**_):
        value=read_manifest(path);imported.append(value['sequence']);return {'inserted_rows':0}
    monkeypatch.setattr(local_sync,'transaction',tx);monkeypatch.setattr(local_sync,'import_bundle',receive)
    result=local_sync.pull_and_import(object(),store,tmp_path/'in',tmp_path/'raw')
    assert imported==[1,2] and result['bundles']==2

def test_tampered_published_head_is_rejected_before_sql(tmp_path,monkeypatch):
    store=tmp_path/'private';transport=FolderTransport(store)
    path=write_bundle(tmp_path/'out',[],stream_id='source',sequence=1,scope=SCOPE)
    transport.publish(path)
    headpath=store/'heads/source.json';head=json.loads(headpath.read_text());head['manifest_hash']='a'*64;headpath.write_text(json.dumps(head))
    monkeypatch.setattr(local_sync,'import_bundle',lambda *a,**kw:pytest.fail('tampered data must not reach SQL'))
    with pytest.raises(ValueError,match='hash'):local_sync.pull_and_import(object(),store,tmp_path/'in',tmp_path/'raw')
