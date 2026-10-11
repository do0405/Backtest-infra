from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from backtest_data.catalog.models import Issuer
from backtest_data.fundamentals.models import Filing, FinancialSourceDocument, SourceFinancialFact


pytestmark = pytest.mark.integration


def test_financial_source_value_context_and_revision_persist_without_document_archive(db_session):
    issuer = Issuer(market="US", source_identifier="CIK0000000001", name="Example")
    db_session.add(issuer)
    db_session.flush()
    timestamp = datetime(2020, 3, 1, tzinfo=timezone.utc)
    filing = Filing(issuer_id=issuer.id, source="sec", accession="0000000001-20-000001", form="10-K",
                    published_at=timestamp, available_at=timestamp, captured_at=timestamp, evidence={})
    db_session.add(filing)
    db_session.flush()
    document = FinancialSourceDocument(filing_id=filing.id, content_hash="a" * 64, source_url="https://www.sec.gov/example",
                  available_at=timestamp, captured_at=timestamp, extraction_complete=True, inventory={"numeric": 1}, parser_version="v1")
    db_session.add(document)
    db_session.flush()
    fact = SourceFinancialFact(document_id=document.id, issuer_id=issuer.id, concept="custom:UsefulNote", account_name="Useful note",
            context_id="C1", dimensions={"segment": "North"}, unit="USD", original_unit="iso4217:USD",
            value=Decimal("123456789.123456789"), raw_value="123456789.123456789", is_nil=False,
            period_kind="instant", scope="consolidated", available_at=timestamp, captured_at=timestamp,
            extracted_at=timestamp, first_seen_at=timestamp, signature="a" * 64, evidence={})
    db_session.add(fact)
    db_session.flush()
    loaded = db_session.scalar(select(SourceFinancialFact).where(SourceFinancialFact.id == fact.id))
    assert loaded.value == Decimal("123456789.123456789")
    assert loaded.dimensions == {"segment": "North"}
    assert not hasattr(document, "storage_key")
    with pytest.raises(DBAPIError, match="immutable"):
        with db_session.begin_nested():
            db_session.execute(text("UPDATE source_financial_fact SET value = 0 WHERE id = :id"), {"id": fact.id})
