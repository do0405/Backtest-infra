from datetime import datetime,timezone

import pytest

from backtest_data.remote.bundle import write_bundle,digest
from backtest_data.remote.folder import FolderTransport


def test_folder_validation_retains_bounded_rows_and_visits_every_row(tmp_path,monkeypatch):
    from backtest_data.remote import folder
    count=200
    path=write_bundle(tmp_path/'source',({'table':'issuer','values':{'id':str(i),'market':'US',
        'source_identifier':str(i),'name':'Fixture'}} for i in range(count)),stream_id='fixture',sequence=1,
        scope={'target_count':17758,'output_start':'2016-01-01','output_end':'2026-10-07'},
        created_at=datetime(2026,10,11,tzinfo=timezone.utc))
    original=folder.iter_bundle_rows
    class Tracked(dict):
        live=0;peak=0;visited=0
        def __init__(self,row):
            super().__init__(row)
            Tracked.live+=1;Tracked.visited+=1;Tracked.peak=max(Tracked.peak,Tracked.live)
        def __del__(self):Tracked.live-=1
    def tracked(*args,**kwargs):
        for row in original(*args,**kwargs):yield Tracked(row)
    monkeypatch.setattr(folder,'iter_bundle_rows',tracked)
    transport=FolderTransport(tmp_path/'transport')
    head=transport.publish(path)
    assert Tracked.visited==count*2 and Tracked.peak<=2 and Tracked.live==0
    before=Tracked.visited
    transport.pull(head['bundle_id'],tmp_path/'destination',expected_manifest_hash=digest(path/'manifest.json'))
    assert Tracked.visited-before==count*2 and Tracked.peak<=2 and Tracked.live==0


def test_late_validation_failure_never_publishes_a_head(tmp_path,monkeypatch):
    from backtest_data.remote import folder
    path=write_bundle(tmp_path/'source',[],stream_id='fixture',sequence=1,
        scope={'target_count':17758,'output_start':'2016-01-01','output_end':'2026-10-07'})
    def reject_late(*args,**kwargs):
        yield {'validated':'first'}
        raise ValueError('last row is invalid')
    monkeypatch.setattr(folder,'iter_bundle_rows',reject_late)
    with pytest.raises(ValueError,match='last row'):
        FolderTransport(tmp_path/'transport').publish(path)
    assert not (tmp_path/'transport/heads').exists()
