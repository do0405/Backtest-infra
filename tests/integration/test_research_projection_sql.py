from datetime import date,datetime,timezone
from decimal import Decimal
import pytest
from sqlalchemy import select
from backtest_data.db import new_id
from backtest_data.catalog.models import Security,SourceContract
from backtest_data.coverage.models import CoverageRun,CoverageMember
from backtest_data.ingestion.models import TargetUniverseManifest,RawObject
from backtest_data.prices.models import PriceObservation
from backtest_data.releases.models import SelectionDecision
from backtest_research.projection import SQLProjector,identity_for

pytestmark=pytest.mark.integration
CLOCK=datetime(2026,10,11,tzinfo=timezone.utc)
CUTOFF=datetime(2026,10,8,4,tzinfo=timezone.utc)


def setup_rows(session):
    manifest=TargetUniverseManifest(content_hash=new_id().replace('-','')*2,scope={},members=[],gaps=[])
    security=Security(market='US',source_identifier=new_id(),name='Projection fixture',security_type='common_stock')
    contract=SourceContract(source='alpaca',domain='prices',market='US',version=new_id(),qualification='contract_defined',
        specification={'endpoint':'/v2/stocks/{symbol}/bars','adjustment':'raw'},content_hash='a'*64)
    raw=RawObject(content_hash=new_id().replace('-','')*2,storage_key=new_id()+'.gz',byte_count=1)
    session.add_all([manifest,security,contract,raw]);session.flush()
    run=CoverageRun(manifest_id=manifest.id,manifest_hash=manifest.content_hash,policy_version='whole_target_coverage_v1',
        output_start=date(2016,1,1),output_end=date(2026,10,7),watermark=CLOCK,target_count=1,details={})
    session.add(run);session.flush()
    session.add(CoverageMember(id=new_id(),run_id=run.id,ordinal=1,security_id=security.id,market='US',member={}))
    session.flush()
    selection=dict(security_id=security.id,source_contract_id=contract.id,basis='as_traded',feed='sip',
        venue='US_CONSOLIDATED',trade_scope='daily',currency='USD',quantity_unit='shares')
    selection['series_key']=identity_for(selection,contract).key
    def add(day,price,status='accepted',captured=CLOCK):
        row=PriceObservation(**{k:v for k,v in selection.items() if k!='series_key'},raw_id=raw.id,session=day,
            open=Decimal(price),high=Decimal(price),low=Decimal(price),close=Decimal(price),volume=Decimal(100),
            notional=None,signature=new_id().replace('-','')*2,available_at=captured,captured_at=captured,evidence={})
        session.add(row);session.flush()
        session.add(SelectionDecision(record_domain='price_observation',record_id=row.id,status=status,
            rule_version='raw_price_structure_v1',evidence_grade='contract_verified',reason='fixture',actor='test',recorded_at=CLOCK))
        session.flush()
        return row
    return run,selection,add


def test_missing_requested_tail_is_not_silently_shortened(db_session,tmp_path):
    run,selection,add=setup_rows(db_session)
    add(date(2026,10,5),'10')
    projector=SQLProjector(None,tmp_path,run_id=run.id,origin='test_fixture')
    data=projector.in_session(db_session,[selection],date(2026,10,5),date(2026,10,7),CUTOFF,CLOCK)
    assert data.sessions==(date(2026,10,5),date(2026,10,6),date(2026,10,7))
    assert 'missing_selected_series_sessions' in data.quality_gaps


def test_newest_quarantined_revision_does_not_fall_back_and_future_day_is_hidden(db_session,tmp_path):
    run,selection,add=setup_rows(db_session)
    add(date(2026,10,5),'10')
    from datetime import timedelta
    add(date(2026,10,6),'12',captured=CLOCK-timedelta(hours=1))
    add(date(2026,10,6),'13',status='quarantined')
    add(date(2026,10,7),'14')
    projector=SQLProjector(None,tmp_path,run_id=run.id,origin='test_fixture')
    data=projector.in_session(db_session,[selection],date(2026,10,5),date(2026,10,7),
        datetime(2026,10,8,3,tzinfo=timezone.utc),CLOCK)
    assert [b.session for b in data.bars]==[date(2026,10,5)]
    assert 'latest_source_revision_unavailable' in data.quality_gaps
    assert data.sessions[-1]==date(2026,10,6)


def test_calendar_conflict_cannot_erase_a_selected_series(db_session,tmp_path,monkeypatch):
    run,selection,add=setup_rows(db_session)
    add(date(2026,10,5),'10')
    monkeypatch.setattr('backtest_research.projection.load_calendar_capture',lambda *a,**k:{
        'rows':[{'date':'2026-10-06','status':'open'}],'capture_id':'calendar-fixture',
        'raw_id':'raw-fixture','body_hash':'c'*64})
    projector=SQLProjector(None,tmp_path,run_id=run.id,origin='test_fixture')
    with pytest.raises(ValueError,match='selected series'):
        projector.in_session(db_session,[selection],date(2026,10,5),date(2026,10,7),CUTOFF,CLOCK)
