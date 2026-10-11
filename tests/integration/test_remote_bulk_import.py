import uuid
from datetime import datetime,timezone
import pytest
from sqlalchemy import select
pytestmark=pytest.mark.integration


def test_large_shared_parent_group_preserves_values_and_rolls_back_natural_key_conflict(db_session,tmp_path):
 from backtest_data.catalog.models import Issuer
 from backtest_data.remote.bundle import write_bundle
 from backtest_data.remote.sql import import_bundle
 from backtest_data.remote.models import RemoteImportReceipt
 from backtest_data.remote.registry import wire_row
 now=datetime(2026,10,11,5,0,0,123456,tzinfo=timezone.utc)
 rows=[]
 for i in range(205):
  v=Issuer(id=str(uuid.uuid4()),market='US',source_identifier=uuid.uuid4().hex,name='Original '+str(i))
  db_session.add(v)
 db_session.flush()
 values=list(db_session.scalars(select(Issuer).where(Issuer.name.like('Original %'))))
 rows=[wire_row({'table':'issuer','values':{c.name:getattr(v,c.name) for c in Issuer.__table__.c}}) for v in values]
 scope={'target_count':17758,'output_start':'2016-01-01','output_end':'2026-10-07'}
 bundle=write_bundle(tmp_path/'ok',rows,stream_id='bulk-'+uuid.uuid4().hex,sequence=1,scope=scope,created_at=now)
 assert import_bundle(db_session,bundle)['inserted_rows']==0
 fresh=[{'table':'issuer','values':{**rows[0]['values'],'id':str(uuid.uuid4()),'source_identifier':uuid.uuid4().hex,'name':'New original '+str(i)}} for i in range(205)]
 new=write_bundle(tmp_path/'new',fresh,stream_id='new-bulk-'+uuid.uuid4().hex,sequence=1,scope=scope,created_at=now)
 assert import_bundle(db_session,new)['inserted_rows']==205
 assert all(db_session.get(Issuer,row['values']['id']).name==row['values']['name'] for row in fresh)
 conflict={**rows[0],'values':{**rows[0]['values'],'id':str(uuid.uuid4())}}
 additions=[{'table':'issuer','values':{**rows[1]['values'],'id':str(uuid.uuid4()),'source_identifier':uuid.uuid4().hex}} for _ in range(204)]
 bad=write_bundle(tmp_path/'bad',additions+[conflict],stream_id='conflict-'+uuid.uuid4().hex,sequence=1,scope=scope,created_at=now)
 with pytest.raises(ValueError,match='natural key'):import_bundle(db_session,bad)
 assert all(db_session.get(Issuer,row['values']['id']) is None for row in additions)
 assert db_session.scalar(select(RemoteImportReceipt).where(RemoteImportReceipt.stream_id.like('conflict-%'))) is None
