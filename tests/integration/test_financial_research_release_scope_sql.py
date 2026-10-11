"""Research scopes preserve the full target and close whole source documents."""
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from backtest_data.catalog.models import Security
from backtest_data.db import utcnow
from backtest_data.fundamentals.models import DerivedFact, SourceFinancialFact
from backtest_data.fundamentals.storage import store_extraction
from backtest_data.fundamentals.xbrl import parse_xbrl
from backtest_data.ingestion.models import TargetUniverseManifest
from backtest_data.releases.build import build_release, collect_candidate
from backtest_data.releases.models import DatasetRelease
from backtest_data.releases.query_context import definition_registry
from backtest_data.releases.validation import frozen_rows
from test_financial_publication_evidence import CAPTURE, XML, append, stored_document


pytestmark = pytest.mark.integration


def target(session, issuer_id):
    security = Security(issuer_id=issuer_id, market="US", source_identifier="publication-target-one",
        security_type="common_stock", name="Original full target one")
    missing = Security(market="US", source_identifier="publication-target-two", security_type="common_stock",
        name="Original full target two")
    session.add_all([security, missing])
    session.flush()
    result = TargetUniverseManifest(content_hash="a" * 64, scope={"markets": ["US"], "historical_universe_complete": False},
        members=[{"security_id": security.id}, {"security_id": missing.id}], gaps=[])
    session.add(result)
    session.flush()
    return result


def scope(document, *, total=3, watermark=None):
    return {"document_ids": [document.id], "audit_watermark": watermark or utcnow() + timedelta(seconds=1),
        "audit_counts": {"total_documents": total, "verified_documents": 1, "missing_documents": total - 1}}


def test_scoped_v6_seals_complete_document_evidence_and_preserves_the_original_target(db_session, tmp_path):
    document, filing, facts, evidence = stored_document(db_session)
    audit_watermark = utcnow()
    proof = append(db_session, document, filing, evidence)
    original_target = target(db_session, filing.issuer_id)
    result = build_release(db_session, original_target.id, raw_root=tmp_path, query_version="release_query_v6",
        financial_scope=scope(document, watermark=audit_watermark))
    release = db_session.get(DatasetRelease, result["release_id"])
    rows = frozen_rows(db_session, release)
    assert set(rows["source_financial_fact"]) == {row.id for row in facts}
    assert set(rows["financial_publication_evidence"]) == {proof.id}
    assert rows["price_observation"] == {}
    assert release.manifest["target_count"] == len(original_target.members) == 2
    assert release.manifest["target_manifest"]["members"] == original_target.members
    descriptor = release.manifest["research_scope"]
    assert descriptor["included_document_ids"] == [document.id]
    assert descriptor["excluded_document_count"] == 2
    assert descriptor["included_public_counts"]["verified_documents"] == 1
    assert release.manifest["policies"]["query_version"] == "release_query_v6"
    assert proof.recorded_at >= audit_watermark


def test_relevant_v6_derived_inputs_expand_complete_dependency_document_inventory(db_session):
    document, filing, facts, evidence = stored_document(db_session)
    append(db_session, document, filing, evidence)
    other_xml = XML.replace(b"</xbrl>", b'<c:Other contextRef="ctx" unitRef="USD" decimals="0">200</c:Other></xbrl>')
    other = store_extraction(db_session, issuer_id=filing.issuer_id, source="sec", accession="dependency-document",
        form="10-K", source_url="https://www.sec.gov/dependency.xml", content=other_xml,
        result=parse_xbrl(other_xml), available_at=CAPTURE, captured_at=CAPTURE)
    other_facts = list(db_session.scalars(select(SourceFinancialFact).where(SourceFinancialFact.document_id == other.id)))
    definition = definition_registry()["revenue_ttm"]
    audit_watermark = utcnow()
    now = utcnow()
    metric = DerivedFact(entity_id=filing.issuer_id, entity_type="issuer", metric_id="revenue_ttm",
        definition_version=definition["definition_version"], definition_hash=definition["definition_hash"], signature="b" * 64,
        value=Decimal(300), unit="USD", status="ok", reason=None, period={"period_end": "2023-12-31"},
        basis={"numeric_policy": definition["numeric_policy"], "disclosure_profile": "periodic_report",
            "query_version": "release_query_v6", "public_availability_policy": "financial_public_pit_v1"},
        input_refs=[{"domain": "source_financial_fact", "id": facts[0].id},
            {"domain": "source_financial_fact", "id": other_facts[0].id}],
        available_at=CAPTURE, captured_at=CAPTURE, materialized_at=now, evaluation_as_of=now)
    db_session.add(metric)
    db_session.flush()
    original_target = target(db_session, filing.issuer_id)
    rows, _, _, _ = collect_candidate(db_session, original_target, query_version="release_query_v6",
        financial_scope=scope(document, watermark=audit_watermark))
    assert set(rows["financial_source_document"]) == {document.id, other.id}
    assert {row.id for row in other_facts}.issubset(rows["source_financial_fact"])
    assert metric.id in rows["derived_fact"]
    legacy, _, _, _ = collect_candidate(db_session, original_target)
    assert metric.id not in legacy["derived_fact"]


def test_audit_watermark_cannot_silently_clip_later_source_extraction_rows(db_session):
    document, filing, _, _ = stored_document(db_session)
    original_target = target(db_session, filing.issuer_id)
    with pytest.raises(ValueError, match="financial_scope_document_after_audit_watermark"):
        collect_candidate(db_session, original_target, query_version="release_query_v6",
            financial_scope=scope(document, watermark=CAPTURE))


def test_v5_default_candidate_shape_remains_free_of_publication_evidence(db_session):
    document, filing, _, evidence = stored_document(db_session)
    append(db_session, document, filing, evidence)
    original_target = target(db_session, filing.issuer_id)
    rows, _, _, _ = collect_candidate(db_session, original_target)
    assert "financial_publication_evidence" not in rows


def test_v6_scoped_budget_counts_the_original_catalog_even_without_price_rows(db_session, monkeypatch):
    import backtest_data.releases.build as module
    document, filing, _, _ = stored_document(db_session)
    original_target = target(db_session, filing.issuer_id)
    monkeypatch.setattr(module, "MAX_RELEASE_ROWS", 3)
    with pytest.raises(ValueError, match="release_row_budget_exceeded"):
        collect_candidate(db_session, original_target, query_version="release_query_v6", financial_scope=scope(document))
