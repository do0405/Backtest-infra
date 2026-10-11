from datetime import timedelta
import importlib
from uuid import uuid4

import pytest
from sqlalchemy import select

from backtest_data.catalog.models import Issuer
from backtest_data.db import utcnow
from backtest_data.fundamentals.models import FundamentalFact, SourceFinancialFact
from backtest_data.fundamentals.storage import store_extraction
from backtest_data.fundamentals.xbrl import parse_xbrl
from backtest_data.releases.models import ValidationIssue

pytestmark = pytest.mark.integration


def module():
    try:
        return importlib.import_module('backtest_data.fundamentals.completion_storage')
    except ModuleNotFoundError:
        pytest.fail('financial completion SQL module is missing')


def instance():
    return b'''<xbrl xmlns="http://www.xbrl.org/2003/instance"
      xmlns:d="http://xbrl.sec.gov/dei/2024" xmlns:g="http://fasb.org/us-gaap/2024"
      xmlns:u="http://www.xbrl.org/2003/iso4217">
      <context id="focus"><entity><identifier scheme="http://www.sec.gov/CIK">1</identifier></entity>
        <period><startDate>2023-01-01</startDate><endDate>2023-03-31</endDate></period></context>
      <unit id="USD"><measure>u:USD</measure></unit>
      <d:DocumentType contextRef="focus">10-Q</d:DocumentType>
      <d:DocumentFiscalYearFocus contextRef="focus">2023</d:DocumentFiscalYearFocus>
      <d:DocumentFiscalPeriodFocus contextRef="focus">Q1</d:DocumentFiscalPeriodFocus>
      <d:DocumentPeriodEndDate contextRef="focus">2023-03-31</d:DocumentPeriodEndDate>
      <d:CurrentFiscalYearEndDate contextRef="focus">--12-31</d:CurrentFiscalYearEndDate>
      <g:Revenues contextRef="focus" unitRef="USD" decimals="0">100</g:Revenues>
    </xbrl>'''


def seed(session):
    issuer = Issuer(market='US', source_identifier='CIK0000000001', name='Completion ' + uuid4().hex)
    session.add(issuer)
    session.flush()
    clock = utcnow() - timedelta(days=1)
    content = instance()
    document = store_extraction(session, issuer_id=issuer.id, source='sec', accession=uuid4().hex,
        form='10-Q', source_url='https://www.sec.gov/fixture.xml', content=content, result=parse_xbrl(content),
        available_at=clock, captured_at=clock)
    return issuer, document, content


def test_exact_hash_sec_supplement_is_additive_idempotent_and_preserves_original_clocks(db_session):
    m = module()
    issuer, document, content = seed(db_session)
    source = db_session.scalar(select(SourceFinancialFact).where(SourceFinancialFact.document_id == document.id))
    original_clocks = (document.available_at, document.captured_at, source.available_at,
        source.captured_at, source.extracted_at, source.first_seen_at)
    annotation = m.supplement_sec_metadata(db_session, document_id=document.id, content=content,
        captured_at=utcnow())
    repeated = m.supplement_sec_metadata(db_session, document_id=document.id, content=content,
        captured_at=utcnow())
    assert annotation.id == repeated.id
    assert annotation.evidence['metadata']['focus'] == 'Q1'
    assert annotation.evidence['metadata']['document_hash'] == document.content_hash
    assert original_clocks == (document.available_at, document.captured_at, source.available_at,
        source.captured_at, source.extracted_at, source.first_seen_at)
    assert 'sec_fiscal_metadata' not in source.evidence
    assert db_session.scalar(select(SourceFinancialFact).where(SourceFinancialFact.document_id == document.id)).id == source.id
    with pytest.raises(ValueError, match='supplement_document_hash_mismatch'):
        m.supplement_sec_metadata(db_session, document_id=document.id, content=content.replace(b'>100<', b'>101<'),
            captured_at=utcnow())


def test_bounded_completion_materializes_source_bound_fiscal_rows_and_missing_scope_audit(db_session):
    m = module()
    issuer, document, content = seed(db_session)
    m.supplement_sec_metadata(db_session, document_id=document.id, content=content, captured_at=utcnow())
    first = m.materialize_financial_completion(db_session, document_ids=[document.id], max_documents=1)
    assert first.created == 1 and first.documents == 1
    canonical = db_session.scalar(select(FundamentalFact).where(FundamentalFact.issuer_id == issuer.id,
        FundamentalFact.mapping_version == 'financial_semantic_completion_v1'))
    assert canonical.period['kind'] == 'quarter' and canonical.period['fiscal_quarter'] == 1
    assert canonical.basis['accounting_basis'] == 'US_GAAP'
    assert canonical.basis['scope'] == 'unknown'
    assert canonical.basis['qualification_reasons'] == ['unknown_scope']
    assert canonical.available_at == document.available_at
    audit = db_session.scalar(select(ValidationIssue).where(ValidationIssue.record_id == document.id,
        ValidationIssue.rule == 'financial_semantic_completion_v1'))
    assert audit.evidence['reasons']['unknown_scope'] == 1
    assert audit.evidence['historical_public_availability_qualified'] is False
    second = m.materialize_financial_completion(db_session, document_ids=[document.id], max_documents=1)
    assert second.created == 0 and second.reused == 1
    assert db_session.scalars(select(ValidationIssue).where(ValidationIssue.record_id == document.id,
        ValidationIssue.rule == 'financial_semantic_completion_v1')).all() == [audit]


def test_missing_fiscal_metadata_is_persisted_without_inventing_annual_dates(db_session):
    m = module()
    issuer, document, _ = seed(db_session)
    report = m.materialize_financial_completion(db_session, document_ids=[document.id], max_documents=1)
    assert report.reasons['missing_fiscal_metadata'] == 1
    row = db_session.scalar(select(FundamentalFact).where(FundamentalFact.issuer_id == issuer.id,
        FundamentalFact.mapping_version == 'financial_semantic_completion_v1'))
    assert row.period['kind'] == 'duration' and row.basis['fiscal_lineage'] is None
    audit = db_session.scalar(select(ValidationIssue).where(ValidationIssue.record_id == document.id,
        ValidationIssue.rule == 'financial_semantic_completion_v1'))
    assert 'missing_fiscal_metadata' in audit.evidence['reasons']
    from backtest_data.fundamentals.metric_policy import REQUIRED_GROWTH_METRICS, ANNUAL_GROWTH_METRICS
    for metric_id in (*REQUIRED_GROWTH_METRICS, *ANNUAL_GROWTH_METRICS):
        assert metric_id in audit.evidence['metric_qualification_gaps']
        assert 'historical_public_availability_unqualified' in audit.evidence['metric_qualification_gaps'][metric_id]['reasons']


def test_source_budget_defers_the_whole_document_and_never_calls_a_partial_inventory_complete(db_session):
    m = module()
    issuer, document, _ = seed(db_session)
    report = m.materialize_financial_completion(db_session, document_ids=[document.id],
        max_documents=1, max_source_facts=1)
    assert report.scanned == 1  # exactly one declared source is within this budget
    # A zero bound is invalid, so test two facts through a second numeric account.
    content = instance().replace(b'</xbrl>', b'<g:OperatingIncomeLoss contextRef="focus" unitRef="USD" decimals="0">7</g:OperatingIncomeLoss></xbrl>')
    clock = utcnow() - timedelta(days=1)
    second = store_extraction(db_session, issuer_id=issuer.id, source='sec', accession=uuid4().hex,
        form='10-Q', source_url='https://www.sec.gov/second.xml', content=content, result=parse_xbrl(content),
        available_at=clock, captured_at=clock)
    capped = m.materialize_financial_completion(db_session, document_ids=[second.id],
        max_documents=1, max_source_facts=1)
    assert capped.created == 0 and capped.scanned == 0
    assert capped.reasons['completion_source_fact_limit'] == 1
    assert not db_session.scalars(select(FundamentalFact).join(SourceFinancialFact,
        FundamentalFact.source_fact_id == SourceFinancialFact.id).where(SourceFinancialFact.document_id == second.id)).all()
    assert len(db_session.scalars(select(SourceFinancialFact).where(SourceFinancialFact.document_id == second.id)).all()) == 2
    same_budget = m.materialize_financial_completion(db_session, document_ids=[second.id],
        max_documents=1, max_source_facts=1, only_pending=True)
    assert same_budget.documents == 0
    larger_budget = m.materialize_financial_completion(db_session, document_ids=[second.id],
        max_documents=1, max_source_facts=2, only_pending=True)
    assert larger_budget.documents == 1 and larger_budget.scanned == 2
    assert larger_budget.created == 2 and 'completion_source_fact_limit' not in larger_budget.reasons


def test_pending_page_revisits_a_new_metadata_assertion_but_skips_completed_documents(db_session):
    m = module()
    issuer, document, content = seed(db_session)
    m.materialize_financial_completion(db_session, document_ids=[document.id], max_documents=1)
    skipped = m.materialize_financial_completion(db_session, document_ids=[document.id], max_documents=1,
        only_pending=True)
    assert skipped.documents == 0
    m.supplement_sec_metadata(db_session, document_id=document.id, content=content, captured_at=utcnow())
    revisited = m.materialize_financial_completion(db_session, document_ids=[document.id], max_documents=1,
        only_pending=True)
    assert revisited.documents == 1 and revisited.created == 1
    rows = db_session.scalars(select(FundamentalFact).where(FundamentalFact.issuer_id == issuer.id,
        FundamentalFact.mapping_version == 'financial_semantic_completion_v1')).all()
    assert {row.period['kind'] for row in rows} == {'duration', 'quarter'}


def test_completion_worker_guard_prevents_duplicate_sql_workers(pg_engine, tmp_path):
    from sqlalchemy import text
    from backtest_data.fundamentals import completion_worker as worker
    with pg_engine.connect() as owner:
        owner.execute(text('SELECT pg_advisory_lock(:key)'), {'key': worker.WORKER_LOCK_KEY})
        owner.commit()
        try:
            result = worker.run_guarded_completion_worker(pg_engine,
                checkpoint_path=tmp_path / 'locked.json', max_batches=1)
            assert result['status'] == 'blocked'
            assert result['reason'] == 'financial_completion_worker_already_running'
            assert not (tmp_path / 'locked.json').exists()
        finally:
            owner.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': worker.WORKER_LOCK_KEY})
            owner.commit()
