from datetime import datetime, timezone
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import select, func


pytestmark = pytest.mark.integration
STAMP = datetime(2026, 10, 10, 5, tzinfo=timezone.utc)
SCOPE = {"manifest_id": "whole-target", "target_count": 17758, "output_start": "2016-01-01", "output_end": "2026-10-07"}


def fixture_rows():
    from backtest_data.catalog.models import SourceContract
    from backtest_data.ingestion.models import RawObject
    from backtest_data.supplemental.models import SupplementalDocument, SupplementalObservation
    token = str(uuid.uuid4())
    contract = {"id": str(uuid.uuid4()), "source": "remote-test", "version": token, "domain": "funds", "market": "US", "specification": {}, "qualification": "pending", "content_hash": token.replace('-', '').ljust(64, '0'), "created_at": STAMP}
    document = {"id": str(uuid.uuid4()), "source_contract_id": contract["id"], "raw_id": str(uuid.uuid4()), "source": "remote-test", "domain": "funds", "source_url": "https://www.sec.gov/files/example", "content_hash": 'a'*64, "signature": token.replace('-', '').ljust(64, '1'), "captured_at": STAMP, "request_scope": {}, "evidence": {"canonical_eligible": False}}
    observation = {c.name: None for c in SupplementalObservation.__table__.columns}
    observation.update(id=str(uuid.uuid4()), document_id=document["id"], record_kind="holding", entity_key="historical-series", row_key=token,
        value=Decimal("123456789012345678901234.123456789012"), captured_at=STAMP, date_basis="report_only", fields={}, issues=[], canonical_eligible=False,
        signature=token.replace('-', '').ljust(64, '2'))
    return contract, document, observation


def test_import_preserves_clock_and_precision_and_replay_inserts_zero(db_session, tmp_path):
    from backtest_data.remote.bundle import write_bundle
    from backtest_data.remote.sql import import_bundle
    from backtest_data.remote.models import RemoteImportReceipt, RemoteStreamCursor
    from backtest_data.supplemental.models import SupplementalObservation
    from backtest_data.ingestion.models import RawObject
    c, d, o = fixture_rows()
    db_session.add(RawObject(id=d['raw_id'], content_hash=d['content_hash'], storage_key='sha256/aa/'+d['content_hash']+'.gz', byte_count=0, created_at=STAMP)); db_session.flush()
    rows = [{"table": "source_contract", "values": c}, {"table": "supplemental_document", "values": d}, {"table": "supplemental_observation", "values": o}]
    path = write_bundle(tmp_path, rows, stream_id="test-"+str(uuid.uuid4()), sequence=1, scope=SCOPE, created_at=STAMP)
    first = import_bundle(db_session, path, imported_at=datetime(2026,10,11,tzinfo=timezone.utc))
    # Replay needs the receipt identity/hash, not its potentially large saved
    # queue/raw checkpoint. Keep it out of SQL result payloads.
    from sqlalchemy import event
    statements=[]
    def record(connection,cursor,statement,parameters,context,executemany):
        statements.append(statement)
    event.listen(db_session.bind,'before_cursor_execute',record)
    db_session.expire_all()
    second = import_bundle(db_session, path)
    event.remove(db_session.bind,'before_cursor_execute',record)
    receipt_reads=[sql for sql in statements if 'FROM remote_import_receipt' in sql]
    assert receipt_reads and all('remote_import_receipt.checkpoint' not in sql for sql in receipt_reads)
    assert not any('pg_advisory_xact_lock' in sql for sql in statements)
    assert first['inserted_rows'] == 3
    assert second['inserted_rows'] == 0
    observed = db_session.get(SupplementalObservation, o['id'])
    assert observed.captured_at == STAMP
    assert observed.value == o['value']
    assert observed.published_date is None
    assert observed.canonical_eligible is False
    assert db_session.get(RemoteImportReceipt, first['bundle_id']) is not None


def test_missing_parent_rolls_back_rows_and_receipt(db_session, tmp_path):
    from backtest_data.remote.bundle import write_bundle
    from backtest_data.remote.sql import import_bundle
    from backtest_data.remote.models import RemoteImportReceipt
    from backtest_data.catalog.models import SourceContract
    c, d, o = fixture_rows()
    path = write_bundle(tmp_path, [{"table":"source_contract","values":c},{"table":"supplemental_document","values":d}], stream_id="test-"+str(uuid.uuid4()), sequence=1, scope=SCOPE, created_at=STAMP)
    with pytest.raises(ValueError, match="parent"):
        import_bundle(db_session, path)
    assert db_session.get(SourceContract,c['id']) is None
    assert db_session.scalar(select(func.count()).select_from(RemoteImportReceipt).where(RemoteImportReceipt.stream_id.like('test-%'))) == 0


def test_jsonb_source_numeric_metadata_is_preserved_without_binary_float(db_session,tmp_path):
    import json
    from sqlalchemy import cast,Text
    from backtest_data.remote.bundle import write_bundle
    from backtest_data.remote.sql import import_bundle,export_delta
    from backtest_data.catalog.models import SourceContract
    c,_,_=fixture_rows();c['specification']={'minimum_interval_seconds':Decimal('0.31'),'exact':Decimal('12345678901234567890.12345678901234567890')}
    path=write_bundle(tmp_path/'incoming',[{'table':'source_contract','values':c}],stream_id='json-'+str(uuid.uuid4()),sequence=1,scope=SCOPE)
    assert import_bundle(db_session,path)['inserted_rows']==1
    stored=db_session.scalar(select(cast(SourceContract.specification,Text)).where(SourceContract.id==c['id']))
    assert json.loads(stored,parse_float=Decimal)==c['specification']
    exported=export_delta(db_session,{'source_contract':[c['id']]},tmp_path/'outgoing',stream_id='json-'+str(uuid.uuid4()),sequence=1,scope=SCOPE)
    from backtest_data.remote.bundle import iter_bundle_rows
    assert list(iter_bundle_rows(exported))[0]['values']['specification']==c['specification']


def test_chunked_export_has_contiguous_stream_and_final_checkpoint_only(db_session,tmp_path):
    from backtest_data.catalog.models import Issuer
    from backtest_data.remote.sql import export_deltas
    from backtest_data.remote.bundle import read_manifest,iter_bundle_rows
    identities=[str(uuid.uuid4()) for _ in range(7)]
    db_session.add_all([Issuer(id=identity,market='US',source_identifier='remote-'+identity,name='Exact issuer') for identity in identities]);db_session.flush()
    paths=export_deltas(db_session,{'issuer':identities},tmp_path,stream_id='chunk-'+str(uuid.uuid4()),sequence=1,
        scope=SCOPE,checkpoint={'all_owned_jobs_retained':True},max_roots_per_bundle=3)
    manifests=[read_manifest(path) for path in paths]
    assert [m['sequence'] for m in manifests]==[1,2,3]
    assert [m['previous_bundle_id'] for m in manifests]==[None,manifests[0]['bundle_id'],manifests[1]['bundle_id']]
    assert [m['checkpoint'] for m in manifests]==[{}, {}, {'all_owned_jobs_retained':True}]
    assert {r['values']['id'] for path in paths for r in iter_bundle_rows(path)}==set(identities)


def test_chunked_export_transfers_shared_raw_parent_once(db_session,tmp_path):
    from sqlalchemy import insert
    from backtest_data.catalog.models import SourceContract
    from backtest_data.ingestion.models import RawObject
    from backtest_data.ingestion.raw_store import RawStore
    from backtest_data.supplemental.models import SupplementalDocument,SupplementalObservation
    from backtest_data.remote.sql import export_deltas
    from backtest_data.remote.bundle import read_manifest,iter_bundle_rows
    c,d,o=fixture_rows();raw_root=tmp_path/'raw';raw=RawStore(raw_root).put(b'exact public source evidence')
    d['content_hash']=raw.content_hash
    db_session.execute(insert(SourceContract).values(**c))
    db_session.add(RawObject(id=d['raw_id'],content_hash=raw.content_hash,storage_key=raw.storage_key,byte_count=raw.byte_count,created_at=STAMP));db_session.flush()
    db_session.execute(insert(SupplementalDocument).values(**d));identities=[]
    for position in range(7):
        item={**o,'id':str(uuid.uuid4()),'row_key':str(uuid.uuid4()),'signature':uuid.uuid4().hex.ljust(64,'a')}
        db_session.execute(insert(SupplementalObservation).values(**item));identities.append(item['id'])
    paths=export_deltas(db_session,{'supplemental_observation':identities},tmp_path/'out',stream_id='raw-chunk-'+str(uuid.uuid4()),
        sequence=1,scope=SCOPE,raw_root=raw_root,max_roots_per_bundle=3)
    assert [sum(f['kind']=='raw' for f in read_manifest(path)['files']) for path in paths]==[1,0,0]
    records=[r for path in paths for r in iter_bundle_rows(path)]
    assert sum(r['table']=='raw_object' for r in records)==1
    assert sum(r['table']=='supplemental_document' for r in records)==1
    assert sum(r['table']=='supplemental_observation' for r in records)==7


def test_full_catalog_bootstrap_selects_original_members_without_numeric_history(db_session):
    from sqlalchemy import insert
    from backtest_data.catalog.models import Security
    from backtest_data.ingestion.models import TargetUniverseManifest
    from backtest_data.remote.bootstrap import bootstrap_roots
    token=uuid.uuid4().hex;members=[];rows=[]
    for index in range(17758):
        identity=str(uuid.uuid4());market='KR' if index<4507 else 'US';identifier='bootstrap-'+token+'-'+str(index)
        members.append({'security_id':identity,'market':market,'identifier':identifier,'symbol':str(index)})
        rows.append({'id':identity,'market':market,'source_identifier':identifier,'security_type':'unknown','name':'Bootstrap fixture','issuer_id':None})
    for start in range(0,len(rows),500):db_session.execute(insert(Security).values(rows[start:start+500]))
    target=TargetUniverseManifest(id=str(uuid.uuid4()),content_hash=token.ljust(64,'a'),scope={'output_start':'2016-01-01','output_end':'2026-10-07','directory_evidence':[]},
        members=members,gaps=[],captured_at=STAMP)
    db_session.add(target);db_session.flush()
    scope,roots=bootstrap_roots(db_session,target.id)
    assert scope['manifest_id']==target.id and scope['target_count']==17758
    assert set(roots['security'])=={m['security_id'] for m in members}
    assert roots['target_universe_manifest']==[target.id]
    assert {'filing','financial_source_document','source_financial_fact','price_observation','collection_job'}.isdisjoint(roots)
