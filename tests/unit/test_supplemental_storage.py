from datetime import date, datetime, timezone
from decimal import Decimal

import pytest


def test_report_date_never_becomes_publication_date():
    from backtest_data.supplemental.storage import normalize_stage_row
    row = normalize_stage_row({'record_kind':'holding','entity_key':'fund:1','row_key':'h:1',
        'report_date':date(2016,1,4),'value':Decimal('123.456789012345678901'),
        'fields':{'value':Decimal('123.456789012345678901')}}, datetime(2026,10,10,tzinfo=timezone.utc))
    assert row['published_date'] is None
    assert row['report_date'] == date(2016,1,4)
    assert row['value'] == Decimal('123.456789012345678901')
    assert row['fields']['value'] == '123.456789012345678901'
    assert row['canonical_eligible'] is False


def test_source_float_is_not_silently_rounded_into_exact_sql():
    from backtest_data.supplemental.storage import normalize_stage_row
    with pytest.raises(ValueError,match='exact'):
        normalize_stage_row({'record_kind':'holding','entity_key':'f','row_key':'h','quantity':0.1},
                            datetime(2026,10,10,tzinfo=timezone.utc))


def test_future_effective_event_is_preserved_without_eligibility():
    from backtest_data.supplemental.storage import normalize_stage_row
    row=normalize_stage_row({'record_kind':'index_announcement','entity_key':'i','row_key':'a',
        'announced_date':date(2026,10,7),'effective_date':date(2026,10,20)},
        datetime(2026,10,10,tzinfo=timezone.utc))
    assert row['effective_date']==date(2026,10,20)
    assert row['canonical_eligible'] is False
    assert 'future_effective_event' in row['issues']


def test_request_evidence_removes_auth_values():
    from backtest_data.supplemental.storage import public_request
    request=public_request({'apikey':'sensitive','page_token':'opaque','date':'2016-01-04'})
    assert 'apikey' not in request
    assert request['date']=='2016-01-04'


def test_nested_request_authentication_is_never_persisted():
    from backtest_data.supplemental.storage import public_request
    safe=public_request({'params':{'apikey':'nested','date':'2016-01-04'},'headers':{'Authorization':'hidden'}})
    assert safe['params']=={'date':'2016-01-04'}
    assert safe['headers']=={}


def test_streamed_archive_is_readable_by_existing_backup_contract(tmp_path):
    from backtest_data.supplemental.storage import archive_file
    from backtest_data.ingestion.raw_store import RawStore
    file=tmp_path/'bulk.zip';file.write_bytes(b'PK'+b'abc'*1000)
    root=tmp_path/'raw'
    archived=archive_file(file,root)
    assert RawStore(root).read(archived['storage_key'])==file.read_bytes()


def test_existing_stream_archive_is_verified_not_trusted(tmp_path):
    from backtest_data.supplemental.storage import archive_file
    path=tmp_path/'bulk';path.write_bytes(b'input')
    root=tmp_path/'raw';metadata=archive_file(path,root)
    (root/metadata['storage_key']).write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='integrity'):
        archive_file(path,root)
