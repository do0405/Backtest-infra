from contextlib import contextmanager
from datetime import date,datetime,timezone
from types import SimpleNamespace

def collector_stub(model):
    # Exercise individual steps without opening credentials/SQL, with the same
    # default acquisition window that the real constructor now establishes.
    c=object.__new__(model)
    c.requested_window=None
    c.start=c.original_start=date(2016,1,1)
    c.end=c.original_end=date(2026,10,7)
    c.nport_quarters=[(2019,4)]
    return c

def test_committed_step_returns_scalar_id_without_touching_detached_document(monkeypatch,tmp_path):
    from backtest_data.supplemental import collector as module
    active=[False]
    class Document:
        @property
        def id(self):
            if not active[0]:raise RuntimeError('detached document access')
            return 'doc-id'
    @contextmanager
    def tx(engine):
        active[0]=True
        try:yield SimpleNamespace(add=lambda row:None)
        finally:active[0]=False
    monkeypatch.setattr(module,'transaction',tx)
    monkeypatch.setattr(module,'register_document',lambda *a,**k:Document())
    monkeypatch.setattr(module,'store_rows',lambda session,document,rows:{'document_id':document.id,'source_rows':1,'inserted':1})
    c=collector_stub(module.SupplementalCollector)
    c.engine=None;c.settings=SimpleNamespace(raw_root=tmp_path);c.path=tmp_path/'state.json'
    c.state={'completed':{},'errors':{},'inserted':0};c.manifest_id='m';c.target_count=17758
    c.start=date(2016,1,1);c.end=date(2026,10,7);c.sec_headers={};c.alpaca_headers=None
    response=SimpleNamespace(content=b'[]',captured_at=datetime(2026,10,10,tzinfo=timezone.utc))
    c.http=SimpleNamespace(fetch=lambda *a,**k:response)
    returned=c.ingest('catalog:test','sec',{'url':'https://www.sec.gov/files/test'},lambda b:[{}])
    assert returned[2]=='doc-id'
    assert c.state['completed']['catalog:test']['inserted']==1

def test_nport_batches_keep_live_iterator_and_verify_archive_once(monkeypatch,tmp_path):
    from backtest_data.supplemental import collector as module
    from backtest_data.supplemental import nport
    calls={'download':0,'iterator':0}
    def download(*args,**kwargs):calls['download']+=1;return {}
    def iterate(*args,**kwargs):
        calls['iterator']+=1
        for n in range(2002):yield {'source_section':'SUBMISSION','source_offset':n}
    @contextmanager
    def tx(engine):yield SimpleNamespace(get=lambda *args:SimpleNamespace(id='doc'))
    monkeypatch.setattr(module,'transaction',tx)
    monkeypatch.setattr(nport,'iter_nport_rows',iterate)
    monkeypatch.setattr(module,'store_rows',lambda session,doc,rows:{'inserted':len(rows),'document_id':'doc'})
    c=collector_stub(module.SupplementalCollector)
    c.engine=None;c.path=tmp_path/'progress.json';c.nport_iterators={};c.sec_headers={}
    c.http=SimpleNamespace(download=download)
    c.state={'nport':{'2019q4_nport':{'document_id':'doc'}},'inserted':0}
    c.can_continue=lambda:True
    c.nport_step();c.nport_step()
    assert calls=={'download':1,'iterator':1}
    assert c.state['nport']['2019q4_nport']['offset']==2000
    assert c.state['inserted']==2000

def test_bootstrap_stops_before_network_when_disk_guard_blocks(tmp_path):
    from backtest_data.supplemental.collector import SupplementalCollector
    c=object.__new__(SupplementalCollector);c.path=tmp_path/'state.json';c.state={}
    c.can_continue=lambda:False
    c.bootstrap()
    assert c.state['phase']=='whole_catalog_and_first_historical_commits'

def test_failed_nport_batch_does_not_advance_or_reuse_exhausted_iterator(monkeypatch,tmp_path):
    import pytest
    from backtest_data.supplemental import collector as module,nport
    calls=[0]
    def rows(*args,**kwargs):
        for n in range(2000):yield {'source_section':'SUBMISSION','source_offset':n}
    def store(session,doc,batch):
        calls[0]+=1
        if calls[0]==1:raise ValueError('invalid source batch')
        return {'inserted':len(batch),'document_id':'doc'}
    @contextmanager
    def tx(engine):yield SimpleNamespace(get=lambda *args:SimpleNamespace(id='doc'))
    monkeypatch.setattr(module,'transaction',tx);monkeypatch.setattr(module,'store_rows',store)
    monkeypatch.setattr(nport,'iter_nport_rows',rows)
    c=collector_stub(module.SupplementalCollector)
    c.engine=None;c.path=tmp_path/'progress.json';c.nport_iterators={};c.sec_headers={};c.can_continue=lambda:True
    c.http=SimpleNamespace(download=lambda *args,**kwargs:{})
    c.state={'nport':{'2019q4_nport':{'document_id':'doc'}},'inserted':0}
    with pytest.raises(ValueError):c.nport_step()
    assert not c.nport_iterators
    assert c.state['nport']['2019q4_nport'].get('offset',0)==0
    c.nport_step()
    assert c.state['nport']['2019q4_nport']['offset']==1000
