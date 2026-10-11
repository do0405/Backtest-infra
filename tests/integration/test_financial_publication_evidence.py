"""Certificates append without rewriting any physical observation history."""
from copy import deepcopy
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from backtest_data.catalog.models import Issuer
from backtest_data.db import utcnow
from backtest_data.fundamentals.models import Filing, SourceFinancialFact
from backtest_data.fundamentals.public_availability import append_publication_evidence, numeric_binding
from backtest_data.fundamentals.publication_models import FinancialPublicationEvidence
from backtest_data.fundamentals.storage import store_extraction
from backtest_data.fundamentals.xbrl import parse_xbrl


pytestmark = pytest.mark.integration
CAPTURE = datetime(2026, 10, 1, tzinfo=timezone.utc)
XML = b'''<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:c="urn:custom" xmlns:i="http://www.xbrl.org/2003/iso4217">
<context id="ctx"><entity><identifier scheme="http://www.sec.gov/CIK">1</identifier></entity><period><instant>2023-12-31</instant></period></context>
<unit id="USD"><measure>i:USD</measure></unit><c:Amount contextRef="ctx" unitRef="USD" decimals="0">100</c:Amount></xbrl>'''


def stored_document(session):
    issuer = Issuer(market="US", source_identifier="CIK0000000001", name="Publication evidence test")
    session.add(issuer)
    session.flush()
    document = store_extraction(session, issuer_id=issuer.id, source="sec", accession="public-evidence-test",
        form="10-K", source_url="https://www.sec.gov/original.xml", content=XML, result=parse_xbrl(XML),
        available_at=CAPTURE, captured_at=CAPTURE)
    filing = session.get(Filing, document.filing_id)
    facts = list(session.scalars(select(SourceFinancialFact).where(SourceFinancialFact.document_id == document.id)))
    binding = numeric_binding(document, facts)
    evidence = {"binding": {"document_id": document.id, "filing_id": filing.id, "issuer_id": filing.issuer_id,
        "source": filing.source, "accession": filing.accession, "document_hash": document.content_hash},
        "proof": {"kind": "sec_dated_feed_original", "public_day": "2024-03-01", "issuer_id": filing.issuer_id,
            "accession": filing.accession, "source_url": "https://www.sec.gov/Archives/edgar/Feed/2024/QTR1/20240301.nc.tar.gz",
            "source_body_hash": "b" * 64, "original_body_hash": document.content_hash,
            "membership_verified": True, "original_version_verified": True, "member_hash": document.content_hash,
            "member_name": "original.xml", "archive_url": "https://www.sec.gov/Archives/edgar/Feed/2024/QTR1/20240301.nc.tar.gz"},
        "numeric_binding": {**binding, "original_digest": binding["sql_digest"]}}
    return document, filing, facts, evidence


def append(session, document, filing, evidence, **changes):
    return append_publication_evidence(session, document=document, filing=filing, status="qualified",
        evidence=evidence, public_day=date(2024, 3, 1), captured_at=CAPTURE, recorded_at=utcnow(), **changes)


def test_idempotent_append_preserves_all_legacy_clocks_and_first_evidence_clocks(db_session):
    document, filing, facts, evidence = stored_document(db_session)
    before = [(row.id, row.available_at, row.captured_at, row.extracted_at, row.first_seen_at, row.signature) for row in facts]
    doc_before = (document.available_at, document.captured_at, document.content_hash)
    filing_before = (filing.available_at, filing.captured_at, filing.published_at)
    first = append(db_session, document, filing, evidence)
    later = utcnow()
    repeat = append_publication_evidence(db_session, document=document, filing=filing, status="qualified",
        evidence=evidence, public_day=date(2024, 3, 1), captured_at=later, recorded_at=later)
    assert first.id == repeat.id
    assert repeat.captured_at == CAPTURE
    assert repeat.recorded_at == first.recorded_at
    assert db_session.scalar(select(func.count()).select_from(FinancialPublicationEvidence)) == 1
    db_session.expire_all()
    assert (document.available_at, document.captured_at, document.content_hash) == doc_before
    assert (filing.available_at, filing.captured_at, filing.published_at) == filing_before
    assert [(row.id, row.available_at, row.captured_at, row.extracted_at, row.first_seen_at, row.signature) for row in facts] == before


@pytest.mark.parametrize("operation", ["UPDATE financial_publication_evidence SET status='rejected' WHERE id=:id",
    "DELETE FROM financial_publication_evidence WHERE id=:id"])
def test_publication_evidence_is_immutable_in_postgres(db_session, operation):
    document, filing, _, evidence = stored_document(db_session)
    row = append(db_session, document, filing, evidence)
    with pytest.raises(DBAPIError, match="immutable"):
        with db_session.begin_nested():
            db_session.execute(text(operation), {"id": row.id})
    assert db_session.get(FinancialPublicationEvidence, row.id).status == "qualified"


def test_claimed_full_digest_is_checked_against_actual_sql_inventory(db_session):
    document, filing, _, evidence = stored_document(db_session)
    evidence["numeric_binding"].update(sql_digest="e" * 64, original_digest="e" * 64)
    with pytest.raises(ValueError, match="publication_sql_numeric_binding_mismatch"):
        append(db_session, document, filing, evidence)
    assert db_session.scalar(select(func.count()).select_from(FinancialPublicationEvidence)) == 0


def test_rejection_has_an_exact_same_document_target_and_never_mutates_the_target(db_session):
    document, filing, _, evidence = stored_document(db_session)
    first = append(db_session, document, filing, evidence)
    rejection = append_publication_evidence(db_session, document=document, filing=filing, status="rejected",
        evidence={"reason": "original_version_mismatch"}, captured_at=CAPTURE, recorded_at=utcnow(),
        supersedes_assertion_hash=first.assertion_hash)
    assert rejection.supersedes_assertion_hash == first.assertion_hash
    assert first.status == "qualified"
    wrong = deepcopy(evidence)
    wrong["binding"]["accession"] = "wrong-receipt"
    with pytest.raises(ValueError, match="publication_binding_mismatch"):
        append(db_session, document, filing, wrong)


def test_unavailable_attempt_is_idempotent_and_does_not_change_the_qualified_certificate(db_session):
    document, filing, _, evidence = stored_document(db_session)
    first = append(db_session, document, filing, evidence)
    arguments = dict(document=document, filing=filing, status="unavailable", evidence={"reason": "blocked_transport"},
        captured_at=CAPTURE, recorded_at=utcnow())
    failed = append_publication_evidence(db_session, **arguments)
    repeat = append_publication_evidence(db_session, **arguments)
    assert failed.id == repeat.id
    assert failed.supersedes_assertion_hash is None
    assert first.status == "qualified"


def test_qualified_recording_cannot_predate_bound_sql_fact_observation(db_session):
    document, filing, _, evidence = stored_document(db_session)
    with pytest.raises(ValueError, match="publication_recording_before_bound_observation"):
        append_publication_evidence(db_session, document=document, filing=filing, status="qualified",
            evidence=evidence, public_day=date(2024, 3, 1), captured_at=CAPTURE, recorded_at=CAPTURE)


def test_withdrawal_cannot_predate_the_target_original_publication(db_session):
    document, filing, _, evidence = stored_document(db_session)
    original = append(db_session, document, filing, evidence)
    evidence["proof"].update(public_day="2024-02-01", event_kind="withdrawal", withdrawal_verified=True)
    evidence['proof']['source_url']=evidence['proof']['archive_url']='https://www.sec.gov/Archives/edgar/Feed/2024/QTR1/20240201.nc.tar.gz'
    with pytest.raises(ValueError, match="withdrawal_before_original_publication"):
        append_publication_evidence(db_session, document=document, filing=filing, status="withdrawal", evidence=evidence,
            public_day=date(2024, 2, 1), captured_at=CAPTURE, recorded_at=utcnow(),
            supersedes_assertion_hash=original.assertion_hash)
