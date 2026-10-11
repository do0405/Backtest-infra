from datetime import date, datetime, timezone
from decimal import Decimal
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from contextlib import contextmanager
from sqlalchemy.orm import Session

pytestmark=pytest.mark.integration

def test_index_sql_immutable_exact_snapshot_separate_from_changes(db_session,tmp_path):
    from backtest_data.indices.models import IndexHistoryRecord
    from backtest_data.indices.storage import store_records
    from backtest_data.supplemental.storage import register_document
    doc=register_document(db_session,source='lseg',domain='index_history',url='https://www.lseg.com/test.csv',request_scope={'original_start':'2016-01-01','original_end':'2026-10-07','target_count':17758},captured_at=datetime(2026,10,10,tzinfo=timezone.utc),raw_root=tmp_path,content=b'Symbol,Company,Weight\nAAA,Issuer,0.123456789123456789\n')
    row={'record_kind':'snapshot_member','index_key':'OFFICIAL:TEST','row_key':'AAA','member_identifier':'AAA','member_name':'Issuer','snapshot_date':date(2016,1,4),'weight_date':date(2016,1,5),'weight':Decimal('0.123456789123456789'),'weight_unit':'percent','published_date':None,'fields':{'source_snapshot_complete':True,'declared_member_count':1},'issues':['publication_unavailable']}
    assert store_records(db_session,doc,[row])['inserted']==1
    assert store_records(db_session,doc,[row])['inserted']==0
    result=db_session.scalar(select(IndexHistoryRecord).where(IndexHistoryRecord.document_id==doc.id))
    assert result.weight==row['weight'] and result.published_date is None
    assert result.snapshot_date!=result.weight_date and not result.canonical_eligible
    with pytest.raises(DBAPIError):
        with db_session.begin_nested():db_session.execute(text('UPDATE index_history_record SET canonical_eligible=true WHERE id=:id'),{'id':result.id})

def _full_manifest(session):
    from backtest_data.ingestion.models import TargetUniverseManifest
    target=TargetUniverseManifest(content_hash='a'*64,
        members=[{'market':'KR','source_identifier':str(n)} for n in range(4507)]+[{'market':'US','source_identifier':str(n)} for n in range(13251)],
        scope={'output_start':'2016-01-01','output_end':'2026-10-07'},gaps=[],captured_at=datetime(2026,10,10,tzinfo=timezone.utc))
    session.add(target);session.flush();return target.id

def _borrow_engine(session,monkeypatch,mod):
    connection=session.connection()
    class Borrow:
        def execute(self,*a,**kw):return connection.execute(*a,**kw)
        def commit(self):pass
        def rollback(self):pass
        def in_transaction(self):return False
    class Engine:
        @contextmanager
        def connect(self):yield Borrow()
    monkeypatch.setattr(mod,'Session',lambda _:Session(connection,join_transaction_mode='create_savepoint'))
    return Engine()

def test_index_pages_resume_atomic_multibatch_idempotently(db_session,tmp_path,monkeypatch):
    from backtest_data.indices import acquisition as mod
    from backtest_data.indices.requests import lseg_catalog_request
    from backtest_data.indices.models import IndexHistoryRecord,IndexAcquisitionState
    manifest_id=_full_manifest(db_session);engine=_borrow_engine(db_session,monkeypatch,mod)
    class API:
        calls=[]
        def fetch(self,request,*,timeout_seconds):
            page=request.payload()['page_number'];self.calls.append(page)
            content=('{"Data":[{"id":'+str(page+1)+',"FamilyMnemonic":"RUSSELL-US","title":"Notice","AnnouncedDate":"2016-01-04T00:00:00","ElectiveDate":"2016-01-05T00:00:00"}],"TotalCount":2}').encode()
            return mod.IndexResponse(content,200,datetime(2026,10,10,tzinfo=timezone.utc),'application/json')
    api=API();plan=[lseg_catalog_request(2016,0,page_size=1)]
    first=mod.collect_index_history(engine,tmp_path,manifest_id=manifest_id,requests=plan,client=api,max_requests=1)
    assert first['status']=='budget_deferred' and first['progress']['inserted']==1
    second=mod.collect_index_history(engine,tmp_path,manifest_id=manifest_id,requests=plan,client=api,max_requests=1)
    assert second['status']=='source_queue_complete' and api.calls==[0,1]
    third=mod.collect_index_history(engine,tmp_path,manifest_id=manifest_id,requests=plan,client=api,max_requests=1)
    assert third['progress']['requests']==0 and third['progress']['inserted']==0
    rows=db_session.scalars(select(IndexHistoryRecord).join(mod.SupplementalDocument).where(mod.SupplementalDocument.domain=='index_history')).all()
    assert len(rows)==2 and all(row.record_kind=='announcement' and row.weight is None for row in rows)
    state=db_session.scalar(select(IndexAcquisitionState).where(IndexAcquisitionState.manifest_id==manifest_id))
    assert state.target_count==17758 and state.output_start==date(2016,1,1)

def test_index_failed_sql_page_does_not_advance_checkpoint(db_session,tmp_path,monkeypatch):
    from backtest_data.indices import acquisition as mod
    from backtest_data.indices.requests import lseg_catalog_request
    from backtest_data.indices.models import IndexHistoryRecord,IndexAcquisitionState
    manifest_id=_full_manifest(db_session);engine=_borrow_engine(db_session,monkeypatch,mod)
    class API:
        def fetch(self,*args,**kwargs):return mod.IndexResponse(b'{"Data":[],"TotalCount":0}',200,datetime(2026,10,10,tzinfo=timezone.utc),'application/json')
    def fail(*args,**kwargs):raise RuntimeError('intentional SQL failure')
    monkeypatch.setattr(mod,'store_records',fail)
    with pytest.raises(RuntimeError):mod.collect_index_history(engine,tmp_path,manifest_id=manifest_id,requests=[lseg_catalog_request(2016)],client=API(),max_requests=1)
    state=db_session.scalar(select(IndexAcquisitionState).where(IndexAcquisitionState.manifest_id==manifest_id))
    assert len(state.cursor['queue'])==1 and state.cursor['completed']==[]
    assert db_session.scalar(select(IndexHistoryRecord.id)) is None

def test_index_denied_response_archived_and_host_not_repeated(db_session,tmp_path,monkeypatch):
    from backtest_data.indices import acquisition as mod
    from backtest_data.indices.requests import nasdaq_catalog_request
    from backtest_data.indices.models import IndexHistoryRecord
    manifest_id=_full_manifest(db_session);engine=_borrow_engine(db_session,monkeypatch,mod)
    class API:
        calls=0
        def fetch(self,*args,**kwargs):
            self.calls+=1;return mod.IndexResponse(b'<h1>Access Denied</h1>',403,datetime(2026,10,10,tzinfo=timezone.utc),'text/html')
    api=API()
    result=mod.collect_index_history(engine,tmp_path,manifest_id=manifest_id,requests=[nasdaq_catalog_request(2016),nasdaq_catalog_request(2017)],client=api,max_requests=2)
    assert api.calls==1 and result['status']=='source_queue_complete' and not result['coverage_complete']
    row=db_session.scalar(select(IndexHistoryRecord).where(IndexHistoryRecord.record_kind=='access_gap'))
    assert row.fields['http_status']==403 and row.published_date is None

def test_existing_official_supplemental_document_projects_once_without_network(db_session,tmp_path,monkeypatch):
    from backtest_data.indices import acquisition as mod
    from backtest_data.indices.models import IndexHistoryRecord
    from backtest_data.supplemental.storage import register_document,store_rows
    manifest_id=_full_manifest(db_session);_borrow_engine(db_session,monkeypatch,mod)
    doc=register_document(db_session,source='spglobal',domain='indices',url='https://press.spglobal.com/2016-release',request_scope={'original_scope':{'target_count':17758}},captured_at=datetime(2026,10,10,tzinfo=timezone.utc),raw_root=tmp_path,content=b'Official S&P change table')
    store_rows(db_session,doc,[{'record_kind':'index_member','entity_key':'S&P 500','row_key':'1:2','member_name':'Issuer','published_date':date(2016,1,22),'announced_date':date(2016,1,22),'effective_date':date(2016,1,29),'fields':{'action':'ADDED','effective_phase':'after_close'},'issues':['weight_unavailable']}])
    class NoSourceCalls:
        def __init__(self,*a,**k):raise AssertionError('Projection must not call source')
    monkeypatch.setattr(mod,'PublicIndexClient',NoSourceCalls)
    result=mod.project_supplemental_history(None,manifest_id=manifest_id,limit=1,budget_seconds=30)
    assert result['progress']['inserted']==1
    again=mod.project_supplemental_history(None,manifest_id=manifest_id,limit=1,budget_seconds=30)
    assert again['progress']['documents']==0
    row=db_session.scalar(select(IndexHistoryRecord).where(IndexHistoryRecord.document_id==doc.id))
    assert row.index_key=='SPDJI:SP500' and row.action=='add' and row.member_identifier is None

@pytest.mark.live
def test_real_official_russell_pdf_stores_hash_and_exact_changes_in_isolated_sql(db_session,tmp_path,monkeypatch,request):
    if not request.config.getoption('--run-live'):pytest.skip('Explicit official public GET required')
    from backtest_data.indices import acquisition as mod
    from backtest_data.indices.models import IndexHistoryRecord
    from backtest_data.indices.requests import public_download_requests
    from backtest_data.ingestion.models import RawObject
    from backtest_data.ingestion.raw_store import RawStore
    manifest_id=_full_manifest(db_session);engine=_borrow_engine(db_session,monkeypatch,mod)
    result=mod.collect_index_history(engine,tmp_path,manifest_id=manifest_id,requests=[public_download_requests()[0]],max_requests=1,budget_seconds=60,job_key='isolated_live_russell_pdf')
    assert result['status']=='source_queue_complete' and result['progress']['inserted']==228 and result['progress']['access_gaps']==0
    rows=db_session.scalars(select(IndexHistoryRecord).where(IndexHistoryRecord.index_key=='FTSE_RUSSELL:RUSSELL_3000')).all()
    assert len(rows)==228 and all(row.record_kind=='change' and row.action=='add' and row.effective_date==date(2025,6,27) for row in rows)
    assert all(row.published_date is None and row.weight is None and not row.canonical_eligible for row in rows)
    document=db_session.get(mod.SupplementalDocument,rows[0].document_id)
    raw=db_session.get(RawObject,document.raw_id)
    assert document.content_hash==raw.content_hash=='83762a06322c86c6faa8d571f5ab571fb0ba654467384c1450cede7ea01fa4fb'
    assert raw.byte_count==213875 and RawStore(tmp_path).read(raw.storage_key).startswith(b'%PDF')
