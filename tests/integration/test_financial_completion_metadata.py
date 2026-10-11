import importlib
from uuid import uuid4

import pytest
from sqlalchemy import select

from backtest_data.db import utcnow
from backtest_data.fundamentals.completion import METADATA_POLICY
from backtest_data.fundamentals.completion_storage import materialize_financial_completion
from backtest_data.fundamentals.storage import store_extraction
from backtest_data.fundamentals.xbrl import parse_xbrl
from backtest_data.ingestion.http import ProviderError, ProviderResponse
from backtest_data.releases.models import ValidationIssue
from test_financial_completion_sql import seed

pytestmark = pytest.mark.integration


def module():
    try:
        return importlib.import_module('backtest_data.fundamentals.completion_metadata')
    except ModuleNotFoundError:
        pytest.fail('financial metadata supplementation stage is missing')


def test_metadata_batch_uses_existing_sec_transport_and_hash_identical_bytes(db_session):
    m = module()
    issuer, document, content = seed(db_session)
    requests = []
    class SEC:
        def fetch(self, url):
            requests.append(url)
            return ProviderResponse(content, 200, utcnow(), 'application/xml')
    result = m.supplement_sec_metadata_batch(db_session, sec=SEC(),
        document_ids=[document.id], max_documents=1)
    assert result.processed == result.reported == 1 and not result.source_blocked
    assert requests == [document.source_url]
    assertion = db_session.scalar(select(ValidationIssue).where(
        ValidationIssue.record_id == document.id, ValidationIssue.rule == METADATA_POLICY))
    assert assertion.evidence['metadata']['document_hash'] == document.content_hash
    repeated = m.supplement_sec_metadata_batch(db_session, sec=SEC(),
        document_ids=[document.id], max_documents=1)
    assert repeated.processed == 0 and len(requests) == 1


def test_first_sec_auth_or_access_failure_stops_the_batch_and_records_a_source_gap(db_session):
    m = module()
    issuer, document, content = seed(db_session)
    second = store_extraction(db_session, issuer_id=issuer.id, source='sec', accession=uuid4().hex,
        form='10-Q', source_url='https://www.sec.gov/another.xml', content=content,
        result=parse_xbrl(content), available_at=document.available_at, captured_at=document.captured_at)
    requests = []
    class SEC:
        def fetch(self, url):
            requests.append(url)
            raise ProviderError('sec', '403')
    result = m.supplement_sec_metadata_batch(db_session, sec=SEC(),
        document_ids=[document.id, second.id], max_documents=2)
    assert result.processed == result.unavailable == 1 and result.source_blocked
    assert len(requests) == 1 and result.reasons == {'sec_source_403': 1}
    assertions = db_session.scalars(select(ValidationIssue).where(
        ValidationIssue.record_id.in_([document.id, second.id]), ValidationIssue.rule == METADATA_POLICY)).all()
    assert len(assertions) == 1
    assert assertions[0].evidence['metadata']['status'] == 'unavailable'
    assert assertions[0].evidence['source_reason'] == 'sec_source_403'


def test_a_changed_missing_reason_does_not_create_another_identical_canonical_version(db_session):
    m = module()
    issuer, document, content = seed(db_session)
    first = materialize_financial_completion(db_session, document_ids=[document.id], max_documents=1)
    assert first.created == 1
    class SEC:
        def fetch(self, url):
            raise ProviderError('sec', '403')
    m.supplement_sec_metadata_batch(db_session, sec=SEC(), document_ids=[document.id], max_documents=1)
    repeated = materialize_financial_completion(db_session, document_ids=[document.id], max_documents=1)
    assert repeated.created == 0 and repeated.reused == 1
    assert repeated.reasons['sec_source_403'] == 1
