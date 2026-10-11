from datetime import date, datetime, timezone
from decimal import Decimal
from sqlalchemy import select,text
from sqlalchemy.exc import DBAPIError
import pytest

def test_supplemental_sql_is_typed_idempotent_and_immutable(db_session,tmp_path):
    from backtest_data.supplemental.storage import register_document,store_rows
    from backtest_data.supplemental.models import SupplementalObservation
    cap=datetime(2026,10,10,tzinfo=timezone.utc)
    doc=register_document(db_session,source='fixture_official',domain='products',
        url='https://www.sec.gov/files/example.tsv',request_scope={'apikey':'never-store','start':'2016-01-01'},
        captured_at=cap,raw_root=tmp_path,content=b'official amount\t1.234567890123456789')
    rows=[{'record_kind':'nport_fund','entity_key':'S0001','row_key':'accession1',
        'report_date':date(2016,1,4),'filing_date':date(2016,2,1),'net_assets':Decimal('1.234567890123456789')}]
    assert store_rows(db_session,doc,rows)['inserted']==1
    assert store_rows(db_session,doc,rows)['inserted']==0
    found=db_session.scalar(select(SupplementalObservation).where(SupplementalObservation.document_id==doc.id))
    assert found.net_assets==Decimal('1.234567890123456789')
    assert found.published_date is None and not found.canonical_eligible
    assert 'apikey' not in doc.request_scope
    with pytest.raises(DBAPIError):
        with db_session.begin_nested():
            db_session.execute(text('UPDATE supplemental_observation SET canonical_eligible=true WHERE id=:id'),{'id':found.id})
    same=register_document(db_session,source='fixture_official',domain='products',url='https://www.sec.gov/files/example.tsv',
        request_scope={'apikey':'never-store','start':'2016-01-01'},captured_at=cap,raw_root=tmp_path,content=b'official amount\t1.234567890123456789')
    assert same.id==doc.id
