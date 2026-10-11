"""Original/amendment public intervals and immutable legacy seals in real SQL."""
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from backtest_data.catalog.models import Issuer,Security
from backtest_data.db import utcnow
from backtest_data.fundamentals.models import Filing,SourceFinancialFact
from backtest_data.fundamentals.public_availability import append_publication_evidence,numeric_binding
from backtest_data.fundamentals.storage import store_extraction
from backtest_data.fundamentals.xbrl import parse_xbrl
from backtest_data.ingestion.models import TargetUniverseManifest
from backtest_data.releases.build import build_release
from backtest_data.releases.query import get_source_accounts,get_fundamentals,get_prices
from backtest_data.releases.query_context import QueryContext

pytestmark=pytest.mark.integration
CAPTURE=datetime(2026,10,1,tzinfo=timezone.utc)
PUBLIC=datetime(2024,3,2,5,tzinfo=timezone.utc)
AMENDMENT=datetime(2024,3,6,5,tzinfo=timezone.utc)


def context(release,cutoff,mode='reconstructed_public',profile='fundamentals_sql'):
    return QueryContext(release,cutoff,mode,profile,'periodic_report' if profile=='fundamentals_sql' else 'not_applicable',True)


def setup(session):
    issuer=Issuer(market='US',source_identifier='CIK0000010000',name='Public PIT fixture')
    session.add(issuer);session.flush()
    security=Security(issuer_id=issuer.id,market='US',source_identifier=str(uuid4()),name='Public PIT fixture',security_type='common_stock')
    session.add(security);session.flush()
    target=TargetUniverseManifest(content_hash=uuid4().hex*2,scope={'historical_universe_complete':False},
        members=[{'security_id':security.id,'issuer_id':issuer.id,'market':'US'}],gaps=[])
    session.add(target);session.flush()
    return issuer,target


def original(session,issuer,value,accession,public_day,*,year=2023,prefix='g'):
    xml=f'''<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:{prefix}="http://fasb.org/us-gaap/2024" xmlns:i="http://www.xbrl.org/2003/iso4217">
    <context id="C"><entity><identifier scheme="http://www.sec.gov/CIK">10000</identifier></entity><period><startDate>{year}-01-01</startDate><endDate>{year}-12-31</endDate></period></context>
    <unit id="USD"><measure>i:USD</measure></unit><{prefix}:RevenueFromContractWithCustomerExcludingAssessedTax contextRef="C" unitRef="USD" decimals="0">{value}</{prefix}:RevenueFromContractWithCustomerExcludingAssessedTax></xbrl>'''.encode()
    document=store_extraction(session,issuer_id=issuer.id,source='sec',accession=accession,form='10-K',
        source_url='https://www.sec.gov/original.xml',content=xml,result=parse_xbrl(xml),
        available_at=CAPTURE,captured_at=CAPTURE,accounting_basis='US_GAAP')
    filing=session.get(Filing,document.filing_id)
    facts=session.scalars(select(SourceFinancialFact).where(SourceFinancialFact.document_id==document.id)).all()
    binding=numeric_binding(document,facts)
    url=f'https://www.sec.gov/Archives/edgar/Feed/{public_day.year}/QTR{(public_day.month-1)//3+1}/{public_day.strftime("%Y%m%d")}.nc.tar.gz'
    evidence={'binding':{'document_id':document.id,'filing_id':filing.id,'issuer_id':issuer.id,'source':'sec',
        'accession':accession,'document_hash':document.content_hash},
        'proof':{'kind':'sec_dated_feed_original','public_day':public_day.isoformat(),'issuer_id':issuer.id,
            'accession':accession,'source_url':url,'source_body_hash':'a'*64,'original_body_hash':document.content_hash,
            'membership_verified':True,'original_version_verified':True,'member_hash':document.content_hash,
            'member_name':'original.xml','archive_url':url},
        'numeric_binding':{**binding,'original_digest':binding['sql_digest']}}
    return document,filing,facts,evidence,public_day


def qualify(session,item):
    document,filing,_,evidence,day=item
    now=utcnow()
    return append_publication_evidence(session,document=document,filing=filing,status='qualified',
        evidence=evidence,public_day=day,captured_at=now,recorded_at=now)


def test_capture_only_original_does_not_override_certified_amendment(db_session):
    issuer,target=setup(db_session)
    first=original(db_session,issuer,'100','0000010000-24-000011',date(2024,3,1))
    correction=original(db_session,issuer,'110','0000010000-24-000012',date(2024,3,5))
    qualify(db_session,correction)
    modern=build_release(db_session,target.id,raw_root=Path('artifacts/tests/no-raw'),query_version='release_query_v6',
        financial_scope={'document_ids':[first[0].id,correction[0].id],'audit_watermark':utcnow()+timedelta(seconds=1),
            'audit_counts':{'total_documents':2,'verified_documents':1,'missing_documents':1}})['release_id']
    historical=get_source_accounts(db_session,[issuer.id],context(modern,AMENDMENT),latest_visible=True)
    assert [row['value'] for row in historical['rows']]==['110']
    for mode in ('reconstructed_public','observed_system'):
        current=context(modern,utcnow()+timedelta(seconds=1),mode)
        latest=get_source_accounts(db_session,[issuer.id],current,latest_visible=True)
        assert latest['rows']==[]
        assert latest['ambiguous_revision_groups']==1
        all_visible=get_source_accounts(db_session,[issuer.id],current)
        assert sorted(row['value'] for row in all_visible['rows'])==['100','110']
        assert {row['public_availability']['status'] for row in all_visible['rows']}=={'capture_only','qualified'}


def seal(session,target,items,query_version='release_query_v6'):
    return build_release(session,target.id,raw_root=Path('artifacts/tests/no-raw'),query_version=query_version,
        financial_scope={'document_ids':[item[0].id for item in items],'audit_watermark':utcnow()+timedelta(seconds=1),
            'audit_counts':{'total_documents':len(items),'verified_documents':len(items),'missing_documents':0}})['release_id']


@pytest.mark.parametrize('query_version',['release_query_v6','release_query_v7'])
def test_public_original_then_amendment_and_observed_capture_preserve_v5(db_session,query_version):
    issuer,target=setup(db_session)
    first=original(db_session,issuer,'100','0000010000-24-000001',date(2024,3,1))
    second=original(db_session,issuer,'110','0000010000-24-000002',date(2024,3,5),prefix='usgaap')
    legacy=build_release(db_session,target.id,raw_root=Path('artifacts/tests/no-raw'))['release_id']
    old=get_source_accounts(db_session,[issuer.id],context(legacy,PUBLIC))
    assert old['rows']==[]
    qualify(db_session,first);qualify(db_session,second)
    from backtest_data.fundamentals.materialize import materialize_canonical
    materialize_canonical(db_session,issuer_ids=[issuer.id])
    modern=seal(db_session,target,[first,second],query_version)
    assert get_source_accounts(db_session,[issuer.id],context(modern,PUBLIC-timedelta(seconds=1)))['rows']==[]
    before_amend=get_source_accounts(db_session,[issuer.id],context(modern,PUBLIC),latest_visible=True)
    assert [row['value'] for row in before_amend['rows']]==['100']
    assert before_amend['rows'][0]['available_at']==CAPTURE.isoformat()
    after_amend=get_source_accounts(db_session,[issuer.id],context(modern,AMENDMENT),latest_visible=True)
    assert [row['value'] for row in after_amend['rows']]==['110']
    assert get_source_accounts(db_session,[issuer.id],context(modern,AMENDMENT,'observed_system'))['rows']==[]
    assert get_source_accounts(db_session,[issuer.id],context(legacy,PUBLIC))==old
    canonical=get_fundamentals(db_session,[issuer.id],context(modern,PUBLIC))
    assert [row['value'] for row in canonical['rows']]==['100']
    with pytest.raises(ValueError,match='outside_release_scope'):
        get_prices(db_session,[],context(modern,PUBLIC,profile='core_price_volume'))


def test_same_public_day_conflicting_versions_do_not_use_capture_order(db_session):
    issuer,target=setup(db_session)
    items=[original(db_session,issuer,str(value),f'0000010000-24-00000{index}',date(2024,3,1))
        for index,value in [(1,100),(2,110)]]
    for item in items:qualify(db_session,item)
    release=seal(db_session,target,items)
    response=get_source_accounts(db_session,[issuer.id],context(release,PUBLIC),latest_visible=True)
    assert response['rows']==[]
    assert response['ambiguous_revision_groups']==1


@pytest.mark.parametrize('query_version',['release_query_v6','release_query_v7'])
def test_public_metric_sql_uses_early_eligible_inputs_and_pins_real_evidence(db_session,query_version):
    from backtest_data.fundamentals.materialize import materialize_metrics
    from backtest_data.fundamentals.models import FundamentalFact,DerivedFact
    from backtest_data.releases.models import SelectionDecision
    from backtest_data.releases.query import get_financial_metrics
    issuer,target=setup(db_session)
    items=[original(db_session,issuer,'100','0000010000-23-000001',date(2023,3,1),year=2022),
        original(db_session,issuer,'110','0000010000-24-000001',date(2024,3,1),year=2023)]
    evidence_ids=[]
    for year,item in zip((2022,2023),items):
        evidence_ids.append(qualify(db_session,item).id)
        source=item[2][0]
        now=utcnow()
        canonical=FundamentalFact(issuer_id=issuer.id,source_fact_id=source.id,metric_id='revenue',
            mapping_version='fundamental_mapping_v1',value=source.value,unit='USD',
            period={'kind':'annual','start':f'{year}-01-01','end':f'{year}-12-31','fiscal_year':year,'fiscal_cycle':str(year)},
            basis={'scope':'consolidated','currency':'USD','accounting_basis':'US_GAAP','fiscal_lineage':'fixture-calendar',
                'qualification_evidence':['test:independently-qualified-annual-period-and-scope']},
            available_at=CAPTURE,captured_at=CAPTURE,materialized_at=now)
        db_session.add(canonical);db_session.flush()
        db_session.add(SelectionDecision(record_domain='fundamental_fact',record_id=canonical.id,status='accepted',
            rule_version='fundamental_mapping_v1',evidence_grade='research_only',reason='fixture qualification',actor='test',recorded_at=utcnow()))
        db_session.flush()
    result=materialize_metrics(db_session,as_of=PUBLIC,query_version=query_version,
        issuer_ids=[issuer.id],metric_ids=['growth_revenue_fy_yoy'])
    assert result.successful==1
    row=db_session.scalar(select(DerivedFact).where(DerivedFact.entity_id==issuer.id))
    assert str(row.value)=='0.1'
    assert row.captured_at==CAPTURE and row.materialized_at>PUBLIC
    assert {ref['id'] for ref in row.input_refs if ref['domain']=='financial_publication_evidence'}==set(evidence_ids)
    repeat=materialize_metrics(db_session,as_of=PUBLIC,query_version=query_version,
        issuer_ids=[issuer.id],metric_ids=['growth_revenue_fy_yoy'])
    assert repeat.created==0 and repeat.reused==1
    modern=seal(db_session,target,items,query_version)
    public=get_financial_metrics(db_session,[issuer.id],context(modern,PUBLIC),['growth_revenue_fy_yoy'])
    assert public['rows'][0]['value']=='0.1'
    observed=get_financial_metrics(db_session,[issuer.id],context(modern,PUBLIC,'observed_system'),['growth_revenue_fy_yoy'])
    assert observed['rows'][0]['status']=='unavailable'
    # New v6 rows do not pollute or prevent a later legacy v5 seal.
    assert build_release(db_session,target.id,raw_root=Path('artifacts/tests/no-raw'))['status']=='sealed'


def test_v7_financial_scope_never_admits_capture_only_original_or_future_correction(db_session):
    issuer,target=setup(db_session)
    first=original(db_session,issuer,'100','0000010000-24-000021',date(2024,3,1))
    correction=original(db_session,issuer,'110','0000010000-24-000022',date(2024,3,5))
    qualify(db_session,correction)
    release=seal(db_session,target,[first,correction],'release_query_v7')
    assert not get_source_accounts(db_session,[issuer.id],context(release,PUBLIC),latest_visible=True)['rows']
    assert not get_source_accounts(db_session,[issuer.id],context(release,AMENDMENT-timedelta(seconds=1)),latest_visible=True)['rows']
    assert [row['value'] for row in get_source_accounts(db_session,[issuer.id],
        context(release,AMENDMENT),latest_visible=True)['rows']]==['110']
    assert not get_source_accounts(db_session,[issuer.id],context(release,AMENDMENT,'observed_system'))['rows']
