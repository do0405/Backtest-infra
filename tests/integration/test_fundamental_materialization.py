from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from backtest_data.catalog.models import Issuer
from backtest_data.fundamentals.models import Filing, FinancialSourceDocument, SourceFinancialFact, FundamentalFact, DerivedFact
from backtest_data.releases.models import SelectionDecision

pytestmark = pytest.mark.integration
NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)


def pipeline():
    assert importlib.util.find_spec('backtest_data.fundamentals.materialize'), 'SQL canonical/derived pipeline missing'
    from backtest_data.fundamentals import materialize
    return materialize


def seed(session, *, value='100', issuer=None, period_end=date(2023, 3, 31), start=date(2023, 1, 1), available=NOW,
         disclosure_profile='periodic_report',period_kind='duration',instant=None,
         concept='us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax'):
    if issuer is None:
        issuer = Issuer(market='US', source_identifier='materialization:' + str(uuid4()), name='Materialization')
        session.add(issuer)
        session.flush()
    filing = Filing(issuer_id=issuer.id, source='sec', accession=str(uuid4()), form='10-Q',
                    available_at=available, captured_at=available, evidence={'original_publication_verified': True})
    session.add(filing)
    session.flush()
    document = FinancialSourceDocument(filing_id=filing.id, content_hash=uuid4().hex * 2,
        source_url='https://www.sec.gov/test.xml', extraction_complete=True,
        inventory={'counts': {'numeric_nodes': 1, 'extracted_nodes': 1, 'deduplicated_nodes': 0},
                   'contexts': {'c': {'id': 'c'}}, 'units': {'USD': 'iso4217:USD'}}, parser_version='test',
        accounting_basis='unknown', available_at=available, captured_at=available)
    document.disclosure_profile = disclosure_profile
    session.add(document)
    session.flush()
    source = SourceFinancialFact(document_id=document.id, issuer_id=issuer.id,
        concept=concept, account_name='Revenue', context_id='c',
        dimensions={}, period_kind=period_kind, period_start=start, period_end=period_end,instant=instant,scope='unknown',
        unit='USD', original_unit='iso4217:USD', value=Decimal(value), raw_value=value, is_nil=False,
        available_at=available, captured_at=available, extracted_at=available, first_seen_at=available,
        signature=uuid4().hex * 2, evidence={'taxonomy': 'http://fasb.org/us-gaap/2024', 'parse_status': 'ok', 'source_node_ids': ['n']})
    session.add(source)
    session.flush()
    session.add(SelectionDecision(record_domain='source_financial_fact', record_id=source.id, status='accepted',
        rule_version='sql_extraction_inventory_v1', evidence_grade='structural_only', reason='fixture', actor='fixture', recorded_at=available))
    session.flush()
    return issuer, source


def test_all_sql_sources_are_mapped_idempotently_with_original_clocks_and_revisions(db_session):
    module = pipeline()
    issuer, first = seed(db_session)
    _, revision = seed(db_session, issuer=issuer, value='110', available=NOW + timedelta(days=1))
    _, other = seed(db_session)
    result = module.materialize_canonical(db_session, materialized_at=NOW + timedelta(days=2), batch_size=1)
    rows = db_session.scalars(select(FundamentalFact).where(FundamentalFact.source_fact_id.in_([first.id, revision.id, other.id]))).all()
    assert len(rows) == 3 and result.created >= 3
    assert all(row.basis['scope'] == 'unknown' and row.basis['accounting_basis'] == 'unknown' for row in rows)
    assert all(row.period['kind'] == 'duration' and row.basis['mapping_hash'] == result.mapping_hash for row in rows)
    old = {row.id: row.materialized_at for row in rows}
    repeat = module.materialize_canonical(db_session, materialized_at=NOW + timedelta(days=3), batch_size=2)
    assert repeat.created == 0 and repeat.reused >= 3
    assert {row.id: row.materialized_at for row in rows} == old
    decisions = db_session.scalars(select(SelectionDecision).where(SelectionDecision.record_domain == 'fundamental_fact',
        SelectionDecision.record_id.in_(old))).all()
    assert len(decisions) == 3 and all(row.evidence_grade == 'research_only' for row in decisions)
    derived = module.materialize_metrics(db_session, as_of=NOW + timedelta(days=3), materialized_at=NOW + timedelta(days=3))
    assert derived.successful == 0 and derived.unavailable > 0


def qualified_canonical(session, source, quarter, *, available=NOW):
    starts = (date(2023, 1, 1), date(2023, 4, 1), date(2023, 7, 1), date(2023, 10, 1))
    ends = (date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31))
    row = FundamentalFact(issuer_id=source.issuer_id, source_fact_id=source.id, metric_id='revenue',
        mapping_version='qualified_test_v1', value=source.value, unit='USD',
        period={'kind': 'quarter', 'start': starts[quarter-1].isoformat(), 'end': ends[quarter-1].isoformat(),
                'fiscal_year': 2023, 'fiscal_quarter': quarter, 'quarter_index': 2023 * 4 + quarter - 1, 'fiscal_cycle': '2023'},
        basis={'scope': 'consolidated', 'currency': 'USD', 'accounting_basis': 'US_GAAP', 'fiscal_lineage': 'verified-calendar',
               'qualification_evidence': ['fixture:explicit-report-periods'], 'numeric_policy': 'ignored_by_adapter'},
        available_at=available, captured_at=NOW, materialized_at=NOW)
    session.add(row)
    session.flush()
    session.add(SelectionDecision(record_domain='fundamental_fact', record_id=row.id, status='accepted',
        rule_version='fundamental_mapping_v1', evidence_grade='research_only', reason='explicit fixture qualification', actor='fixture', recorded_at=NOW))
    session.flush()
    return row


def test_v7_metrics_reject_uncertified_inputs_even_when_capture_is_visible(db_session):
    issuer=None
    for quarter in range(1,5):
        starts=(date(2023,1,1),date(2023,4,1),date(2023,7,1),date(2023,10,1))
        ends=(date(2023,3,31),date(2023,6,30),date(2023,9,30),date(2023,12,31))
        issuer,source=seed(db_session,issuer=issuer,start=starts[quarter-1],period_end=ends[quarter-1])
        qualified_canonical(db_session,source,quarter)
    report=pipeline().materialize_metrics(db_session,as_of=NOW,materialized_at=NOW,
        issuer_ids=[issuer.id],metric_ids=['revenue_ttm'],query_version='release_query_v7')
    assert report.successful==0
    assert report.reasons['public_availability_certificate_required']==4


def test_future_instant_is_excluded_when_legacy_period_end_is_null(db_session):
    issuer,source=seed(db_session,start=None,period_end=None,period_kind='instant',
        instant=date(2025,12,31),concept='us-gaap:Assets')
    canonical=FundamentalFact(issuer_id=issuer.id,source_fact_id=source.id,metric_id='assets_total',
        mapping_version='qualified_test_v1',value=source.value,unit='USD',
        period={'kind':'instant','end':None,'instant':'2025-12-31'},
        basis={'scope':'consolidated','currency':'USD','accounting_basis':'US_GAAP'},
        available_at=NOW,captured_at=NOW,materialized_at=NOW)
    db_session.add(canonical);db_session.flush()
    db_session.add(SelectionDecision(record_domain='fundamental_fact',record_id=canonical.id,status='accepted',
        rule_version='fundamental_mapping_v1',evidence_grade='research_only',reason='fixture',actor='fixture',recorded_at=NOW))
    report=pipeline().materialize_metrics(db_session,as_of=NOW,materialized_at=NOW,
        issuer_ids=[issuer.id],metric_ids=['revenue_ttm'])
    assert report.successful==0
    assert report.reasons['future_source_input']==1


def test_qualified_sql_metrics_pin_registry_and_actual_input_refs_and_are_idempotent(db_session):
    module = pipeline()
    issuer = None
    canonical = []
    for quarter in range(1, 5):
        starts = (date(2023, 1, 1), date(2023, 4, 1), date(2023, 7, 1), date(2023, 10, 1))
        ends = (date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31))
        issuer, source = seed(db_session, issuer=issuer, value=str(quarter * 100), start=starts[quarter-1], period_end=ends[quarter-1])
        canonical.append(qualified_canonical(db_session, source, quarter))
    first = module.materialize_metrics(db_session, as_of=NOW, materialized_at=NOW)
    rows = db_session.scalars(select(DerivedFact).where(DerivedFact.entity_id == issuer.id, DerivedFact.metric_id == 'revenue_ttm')).all()
    assert len(rows) == 1 and rows[0].value == Decimal(1000) and first.successful >= 1
    from backtest_data.releases.query_context import definition_registry, registered_definition
    assert registered_definition(rows[0]) and rows[0].definition_hash == definition_registry()['revenue_ttm']['definition_hash']
    assert {ref['id'] for ref in rows[0].input_refs if ref['domain'] == 'fundamental_fact'} == {row.id for row in canonical}
    assert {ref['id'] for ref in rows[0].input_refs if ref['domain'] == 'source_financial_fact'} == {row.source_fact_id for row in canonical}
    assert rows[0].available_at == NOW and rows[0].evaluation_as_of == NOW
    assert rows[0].basis['disclosure_profile'] == 'periodic_report'
    second = module.materialize_metrics(db_session, as_of=NOW, materialized_at=NOW + timedelta(days=1))
    assert second.created == 0 and second.reused >= 1
    later = module.materialize_metrics(db_session, as_of=NOW + timedelta(days=2), materialized_at=NOW + timedelta(days=2))
    assert later.created >= 1
    assert len(db_session.scalars(select(DerivedFact).where(DerivedFact.entity_id == issuer.id, DerivedFact.metric_id == 'revenue_ttm')).all()) == 2
    from backtest_data.catalog.models import Security
    from backtest_data.ingestion.models import TargetUniverseManifest
    from backtest_data.releases.build import build_release
    from backtest_data.releases.query import get_financial_metrics
    from backtest_data.releases.query_context import QueryContext
    security = Security(issuer_id=issuer.id, market='US', source_identifier=str(uuid4()), security_type='common_stock', name='Fixture')
    db_session.add(security)
    db_session.flush()
    target = TargetUniverseManifest(content_hash=uuid4().hex * 2, scope={'historical_universe_complete': False},
        members=[{'security_id': security.id, 'issuer_id': issuer.id, 'market': 'US'}], gaps=[])
    db_session.add(target)
    db_session.flush()
    release_id = build_release(db_session, target.id, raw_root=Path('artifacts/tests/no-raw'))['release_id']
    context = QueryContext(release_id, NOW + timedelta(days=3), 'reconstructed_public', 'fundamentals_sql', 'periodic_report', True)
    queried = get_financial_metrics(db_session, [issuer.id], context, ['revenue_ttm'])['rows']
    assert len(queried) == 1 and queried[0]['value'] == '1000' and queried[0]['status'] == 'ok'


def test_mapping_version_hash_collision_and_missing_source_lineage_fail_closed(db_session):
    module = pipeline()
    _, source = seed(db_session)
    module.materialize_canonical(db_session, materialized_at=NOW)
    from dataclasses import replace
    from backtest_data.fundamentals.mapping_catalog import default_catalog
    original = default_catalog()
    changed = replace(original, entries=(replace(original.entries[0], metric='changed'),) + original.entries[1:])
    with pytest.raises(ValueError, match='mapping_version_hash_conflict'):
        module.materialize_canonical(db_session, catalog=changed, materialized_at=NOW)
    row = qualified_canonical(db_session, source, 1)
    row.source_fact_id = str(uuid4())
    with pytest.raises(ValueError, match='source_lineage_missing'):
        module.canonical_to_financial_fact(row, source=source)


def test_later_rejection_blocks_sql_inputs_and_materialization_never_backdates(db_session):
    module = pipeline()
    issuer, source = seed(db_session)
    qualified_canonical(db_session, source, 1)
    db_session.add(SelectionDecision(record_domain='source_financial_fact', record_id=source.id, status='rejected',
        rule_version='source_policy_v2', evidence_grade='research_only', reason='new rejection', actor='fixture',
        recorded_at=NOW + timedelta(hours=1)))
    db_session.flush()
    mapped = module.materialize_canonical(db_session, materialized_at=NOW + timedelta(days=1))
    assert mapped.created == 0 and mapped.ineligible >= 1
    computed = module.materialize_metrics(db_session, as_of=NOW + timedelta(days=1), materialized_at=NOW + timedelta(days=1))
    assert computed.successful == 0
    _, accepted = seed(db_session)
    with pytest.raises(ValueError, match='materialization_precedes_input_clock'):
        module.materialize_canonical(db_session, materialized_at=NOW - timedelta(days=1))


def test_observed_after_cutoff_failure_is_not_persisted_under_eligible_signature(db_session):
    module = pipeline()
    issuer = None
    for quarter in range(1, 5):
        starts = (date(2023, 1, 1), date(2023, 4, 1), date(2023, 7, 1), date(2023, 10, 1))
        ends = (date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31))
        issuer, source = seed(db_session, issuer=issuer, start=starts[quarter-1], period_end=ends[quarter-1])
        qualified_canonical(db_session, source, quarter)
    invalid = module.materialize_metrics(db_session, as_of=NOW, history_mode='observed_system',
        materialized_at=NOW + timedelta(days=1), metric_ids=['revenue_ttm'])
    assert invalid.successful == 0 and invalid.created == 0 and invalid.skipped_cutoff >= 1
    valid = module.materialize_metrics(db_session, as_of=NOW, history_mode='observed_system',
        materialized_at=NOW, metric_ids=['revenue_ttm'])
    assert valid.successful >= 1 and valid.created >= 1


def test_source_and_canonical_period_disagreement_is_ineligible(db_session):
    module = pipeline()
    _, source = seed(db_session)
    row = qualified_canonical(db_session, source, 2)
    with pytest.raises(ValueError, match='canonical_source_period_mismatch'):
        module.canonical_to_financial_fact(row, source=source)


def test_sql_economic_date_cutoff_is_equivalent_in_utc_and_seoul(db_session):
    module = pipeline()
    issuer = None
    for quarter in range(1, 5):
        starts = (date(2023, 1, 1), date(2023, 4, 1), date(2023, 7, 1), date(2023, 10, 1))
        ends = (date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31))
        issuer, source = seed(db_session, issuer=issuer, start=starts[quarter-1], period_end=ends[quarter-1],
                              available=datetime(2023, 1, 1, tzinfo=timezone.utc))
        qualified_canonical(db_session, source, quarter, available=datetime(2023, 1, 1, tzinfo=timezone.utc))
    db_session.flush()
    utc = datetime(2023, 12, 30, 18, tzinfo=timezone.utc)
    seoul = utc.astimezone(timezone(timedelta(hours=9)))
    results = [module.materialize_metrics(db_session, as_of=cutoff, materialized_at=NOW, metric_ids=['revenue_ttm'])
               for cutoff in (utc, seoul)]
    assert results[0].successful == results[1].successful == 0
    assert results[0].reasons == results[1].reasons


def test_unsupported_disclosure_profile_never_materializes_a_financial_result(db_session):
    module = pipeline()
    _, source = seed(db_session, disclosure_profile='event_disclosure')
    row = qualified_canonical(db_session, source, 1)
    result = module.materialize_metrics(db_session, as_of=NOW, materialized_at=NOW, metric_ids=['growth_revenue_qoq'])
    assert result.successful == 0 and result.created == 0
    assert result.reasons.get('unsupported_disclosure_profile') == 1


def test_actual_xml_sql_mapping_keeps_custom_dimensioned_and_nil_evidence(db_session):
    from backtest_data.db import utcnow
    from backtest_data.fundamentals.xbrl import parse_xbrl
    from backtest_data.fundamentals.storage import store_extraction
    issuer = Issuer(market='US', source_identifier='CIK0000000001', name='XML materialization')
    db_session.add(issuer)
    db_session.flush()
    content = b'''<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:g="http://fasb.org/us-gaap/2024"
        xmlns:i="http://www.xbrl.org/2003/iso4217" xmlns:custom="urn:custom"
        xmlns:d="http://xbrl.org/2006/xbrldi" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <context id="duration"><entity><identifier scheme="http://www.sec.gov/CIK">1</identifier></entity>
        <period><startDate>2023-01-01</startDate><endDate>2023-03-31</endDate></period></context>
      <context id="instant"><entity><identifier scheme="http://www.sec.gov/CIK">1</identifier></entity>
        <period><instant>2023-03-31</instant></period></context>
      <context id="dimension"><entity><identifier scheme="http://www.sec.gov/CIK">1</identifier>
        <segment><d:explicitMember dimension="custom:RegionAxis">custom:East</d:explicitMember></segment></entity>
        <period><startDate>2023-01-01</startDate><endDate>2023-03-31</endDate></period></context>
      <unit id="usd"><measure>i:USD</measure></unit>
      <g:RevenueFromContractWithCustomerExcludingAssessedTax contextRef="duration" unitRef="usd" decimals="2">123.45</g:RevenueFromContractWithCustomerExcludingAssessedTax>
      <g:Assets contextRef="instant" unitRef="usd" decimals="0">900</g:Assets>
      <custom:UsefulNote contextRef="duration" unitRef="usd" decimals="0">7</custom:UsefulNote>
      <g:RevenueFromContractWithCustomerExcludingAssessedTax contextRef="dimension" unitRef="usd" decimals="0">40</g:RevenueFromContractWithCustomerExcludingAssessedTax>
      <g:OperatingIncomeLoss contextRef="duration" unitRef="usd" xsi:nil="true" />
    </xbrl>'''
    now = utcnow()
    document = store_extraction(db_session, issuer_id=issuer.id, source='sec', accession=str(uuid4()), form='10-Q',
        source_url='https://www.sec.gov/fixture.xml', content=content, result=parse_xbrl(content),
        available_at=now, captured_at=now)
    result = pipeline().materialize_canonical(db_session, materialized_at=utcnow())
    sources = db_session.scalars(select(SourceFinancialFact).where(SourceFinancialFact.document_id == document.id)).all()
    canonical = db_session.scalars(select(FundamentalFact).where(FundamentalFact.issuer_id == issuer.id)).all()
    assert len(sources) == 5 and len(canonical) == 2
    assert {row.metric_id for row in canonical} == {'revenue', 'assets_total'}
    assert result.unmapped >= 2 and result.unavailable >= 1
    assert all(row.basis['scope'] == 'unknown' and row.basis['fiscal_lineage'] is None for row in canonical)


def test_existing_repeatable_read_session_cannot_bypass_mapper_lock_visibility(pg_engine):
    from sqlalchemy.orm import Session
    with pg_engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
        with Session(connection) as session:
            with pytest.raises(ValueError, match='read_committed_materialization_session_required'):
                pipeline().materialize_canonical(session, materialized_at=NOW)


def test_default_canonical_clock_records_completion_not_invocation(db_session, monkeypatch):
    module=pipeline()
    seed(db_session)
    clock=[NOW+timedelta(seconds=1)]
    monkeypatch.setattr(module,'utcnow',lambda:clock[0])
    original=module.map_source_fact
    def normalize(*args,**kwargs):
        result=original(*args,**kwargs)
        clock[0]=NOW+timedelta(seconds=20)
        return result
    monkeypatch.setattr(module,'map_source_fact',normalize)
    module.materialize_canonical(db_session)
    row=db_session.scalar(select(FundamentalFact))
    assert row.materialized_at==clock[0]


def test_default_scan_defers_newer_inputs_without_rolling_back_other_rows(db_session,monkeypatch):
    module=pipeline()
    issuer,old=seed(db_session)
    seed(db_session,issuer=issuer,available=NOW+timedelta(seconds=10))
    monkeypatch.setattr(module,'utcnow',lambda:NOW)
    report=module.materialize_canonical(db_session,batch_size=1)
    assert report.created==1 and report.ineligible==1
    assert report.reasons['input_newer_than_scan_cutoff']==1
    assert db_session.scalar(select(FundamentalFact)).source_fact_id==old.id


def test_default_metric_finishing_after_observed_cutoff_is_not_a_success(db_session,monkeypatch):
    module=pipeline()
    issuer=None
    for quarter in range(1,5):
        ends=(date(2023,3,31),date(2023,6,30),date(2023,9,30),date(2023,12,31))
        starts=(date(2023,1,1),date(2023,4,1),date(2023,7,1),date(2023,10,1))
        issuer,source=seed(db_session,issuer=issuer,period_end=ends[quarter-1],start=starts[quarter-1])
        qualified_canonical(db_session,source,quarter)
    clock=[NOW]
    monkeypatch.setattr(module,'utcnow',lambda:clock[0])
    original=module.calculate_metric
    def calculate(*args,**kwargs):
        result=original(*args,**kwargs)
        clock[0]=NOW+timedelta(seconds=2)
        return result
    monkeypatch.setattr(module,'calculate_metric',calculate)
    report=module.materialize_metrics(db_session,as_of=NOW+timedelta(seconds=1),
        history_mode='observed_system',metric_ids=['revenue_ttm'])
    assert report.successful==0 and report.created==0 and report.skipped_cutoff==1
