"""Sealed v8 daily metadata queries preserve source clocks and legacy seals."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from backtest_data.db import utcnow
from backtest_data.fundamentals.daily_publication import append_daily_publication_evidence, derive_daily_publication
from backtest_data.fundamentals.materialize import materialize_metrics
from backtest_data.fundamentals.models import Filing
from backtest_data.prices.models import PriceObservation
from backtest_data.releases.build import build_release
from backtest_data.releases.models import SelectionDecision
from backtest_data.releases.query import get_financial_metrics, get_fundamentals, get_prices, get_source_accounts
from backtest_data.releases.query_context import QueryContext
from test_metric_public_revision_gate import fixture_input, fixture_target, GROWTH

pytestmark = pytest.mark.integration
VERSION = 'release_query_v8_daily'
MONDAY = datetime(2024,2,5,14,30,tzinfo=timezone.utc)


def context(release, cutoff=MONDAY, *, mode='reconstructed_public', profile='fundamentals_sql'):
    return QueryContext(release,cutoff,mode,profile,'periodic_report' if profile=='fundamentals_sql' else 'not_applicable',True)


def daily_input(session,issuer,value,day,**kwargs):
    document = fixture_input(session,issuer,value,day,qualified=False,**kwargs)
    filing = session.get(Filing,document.filing_id)
    resolution = derive_daily_publication(document,filing,market='US',metadata_records=[
        {'accessionNumber':filing.accession,'filingDate':day.isoformat()}])
    proof = append_daily_publication_evidence(session,document=document,filing=filing,resolution=resolution)
    return document,proof


def seal(session,target,documents,version=VERSION):
    return build_release(session,target.id,raw_root=Path('artifacts/tests/no-raw'),query_version=version,
        financial_scope={'document_ids':[doc.id for doc in documents], 'audit_watermark':utcnow()+timedelta(seconds=1),
            'audit_counts':{'total_documents':len(documents),'verified_documents':len(documents),'missing_documents':0}})


def test_v8_metadata_only_original_then_correction_and_legacy_release_remains_exact(db_session):
    issuer,target = fixture_target(db_session)
    first,_ = daily_input(db_session,issuer,'100',date(2024,2,2))
    correction,_ = daily_input(db_session,issuer,'80',date(2024,4,5))
    legacy = build_release(db_session,target.id,raw_root=Path('artifacts/tests/no-raw'))['release_id']
    old = get_source_accounts(db_session,[issuer.id],context(legacy))
    modern = seal(db_session,target,[first,correction])
    release = modern['release_id']
    assert modern['counts']['financial_daily_publication_evidence'] == 2
    assert modern['counts']['financial_publication_evidence'] == 0
    assert not get_source_accounts(db_session,[issuer.id],context(release,MONDAY-timedelta(seconds=1)))['rows']
    early = get_source_accounts(db_session,[issuer.id],context(release),latest_visible=True)
    assert [row['value'] for row in early['rows']] == ['100']
    assert early['rows'][0]['public_availability']['metadata_public_bound'] == MONDAY.isoformat()
    assert early['rows'][0]['public_availability']['evidence_grade'] == 'metadata_grade'
    after = get_source_accounts(db_session,[issuer.id],context(release,datetime(2024,4,8,13,30,tzinfo=timezone.utc)),latest_visible=True)
    assert [row['value'] for row in after['rows']] == ['80']
    assert [row['value'] for row in get_fundamentals(db_session,[issuer.id],context(release))['rows']] == ['100']
    assert not get_source_accounts(db_session,[issuer.id],context(release,mode='observed_system'))['rows']
    assert get_source_accounts(db_session,[issuer.id],context(legacy)) == old


def test_v8_weekend_correction_suppresses_stale_metric_until_recomputed(db_session):
    issuer,target = fixture_target(db_session)
    baseline,_ = daily_input(db_session,issuer,'100',date(2023,2,3),year=2022)
    original,_ = daily_input(db_session,issuer,'100',date(2024,2,2))
    assert materialize_metrics(db_session,as_of=MONDAY,issuer_ids=[issuer.id],metric_ids=[GROWTH],query_version=VERSION).successful == 1
    amended,_ = daily_input(db_session,issuer,'80',date(2024,2,3))
    sealed = seal(db_session,target,[baseline,original,amended])
    latest = get_source_accounts(db_session,[issuer.id],context(sealed['release_id']),latest_visible=True)
    assert sorted(row['value'] for row in latest['rows']) == ['100','80']
    stale = get_financial_metrics(db_session,[issuer.id],context(sealed['release_id']),[GROWTH])['rows'][0]
    assert stale['status'] == 'unavailable' and stale['value'] is None
    assert materialize_metrics(db_session,as_of=MONDAY,issuer_ids=[issuer.id],metric_ids=[GROWTH],query_version=VERSION).successful == 1
    updated = seal(db_session,target,[baseline,original,amended])
    result = get_financial_metrics(db_session,[issuer.id],context(updated['release_id']),[GROWTH])['rows'][0]
    assert result['value'] == '-0.2'
    assert result['public_availability']['evidence_grade'] == 'metadata_grade'


def test_v8_same_public_day_conflicting_facts_do_not_choose_uuid(db_session):
    issuer,target = fixture_target(db_session)
    first,_ = daily_input(db_session,issuer,'100',date(2024,2,2))
    second,_ = daily_input(db_session,issuer,'80',date(2024,2,2))
    sealed = seal(db_session,target,[first,second])
    result = get_source_accounts(db_session,[issuer.id],context(sealed['release_id']),latest_visible=True)
    assert result['rows'] == []
    assert result['ambiguous_revision_groups'] == 1


def test_v8_price_scope_queries_supplier_adjusted_and_raw_by_explicit_basis(db_session):
    from backtest_data.catalog.models import Security, SourceContract
    from backtest_data.ingestion.models import RawObject, TargetUniverseManifest
    from backtest_data.ingestion.raw_store import RawStore
    session = db_session
    issuer,original_target = fixture_target(session)
    security_id = original_target.members[0]['security_id']
    missing = Security(market='US',source_identifier=str(uuid4()),security_type='unclassified',name='Unresolved target')
    session.add(missing);session.flush()
    target = TargetUniverseManifest(content_hash=uuid4().hex*2,scope={'markets':['US'],'historical_universe_complete':False},
        members=[{'security_id':security_id,'issuer_id':issuer.id,'market':'US'}, {'security_id':missing.id,'market':'US'}],gaps=[])
    raw_root = Path('artifacts/tests/releases')/str(uuid4())
    archived = RawStore(raw_root).put(b'{"fixture":"supplier basis"}')
    raw = RawObject(content_hash=archived.content_hash,storage_key=archived.storage_key,byte_count=archived.byte_count)
    session.add_all([target,raw]);session.flush()
    contracts = []
    for adjustment in ('raw','split'):
        contract = SourceContract(source='alpaca',version=str(uuid4()),domain='prices',market='US',
            qualification='supplier_declared',content_hash=uuid4().hex*2,
            specification={'endpoint':'/v2/stocks/{symbol}/bars','adjustment':adjustment})
        session.add(contract);session.flush();contracts.append(contract)
    day = date(2020,2,28)
    capture = datetime(2026,10,1,tzinfo=timezone.utc)
    for contract,basis,value,volume in [(contracts[0],'as_traded','60','10'), (contracts[1],'provider_adjusted','30','20')]:
        price = PriceObservation(security_id=security_id,source_contract_id=contract.id,raw_id=raw.id,
            session=day,basis=basis,feed='sip',venue='US_CONSOLIDATED',trade_scope='supplier_daily',
            currency='USD',quantity_unit='shares',open=Decimal(value),high=Decimal(value),low=Decimal(value),close=Decimal(value),
            volume=Decimal(volume),signature=uuid4().hex*2,available_at=capture,captured_at=capture,evidence={})
        session.add(price);session.flush()
        session.add(SelectionDecision(record_domain='price_observation',record_id=price.id,status='accepted',
            rule_version='provider_declared_price_structure_v1',evidence_grade='supplier_declared',reason='fixture',actor='test',recorded_at=utcnow()))
    session.flush()
    release = build_release(session,target.id,raw_root=raw_root,query_version=VERSION,
        price_scope={'start':day,'end':day})
    assert release['target_count'] == 2
    assert release['counts']['price_observation'] == 2
    cutoff = datetime(2020,2,29,5,tzinfo=timezone.utc)
    ctx = context(release['release_id'],cutoff,profile='core_price_volume')
    assert len(get_prices(session,[],ctx)['rows']) == 1
    result = get_prices(session,[],ctx,basis='provider_adjusted')['rows']
    assert len(result) == 1 and result[0]['close'] == '30'
    assert result[0]['market_availability']['price_basis_policy']['adjustments'] == ['split']
    assert not get_prices(session,[],context(release['release_id'],cutoff-timedelta(seconds=1),profile='core_price_volume'),basis='provider_adjusted')['rows']
    assert not get_prices(session,[],context(release['release_id'],cutoff,mode='observed_system',profile='core_price_volume'),basis='provider_adjusted')['rows']
