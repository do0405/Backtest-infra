"""A bounded research seal declares its original denominator and complete scope."""
from datetime import datetime, timezone

import pytest

from backtest_data.fundamentals.publication_models import FinancialPublicationEvidence
from backtest_data.ingestion.models import TargetUniverseManifest
from backtest_data.releases import build


WATERMARK = datetime(2026, 10, 1, tzinfo=timezone.utc)


def target():
    return TargetUniverseManifest(id="T", content_hash="a" * 64, members=[{"security_id": "S"}, {"security_id": "S2"}],
        scope={"markets": ["US", "KR"]}, gaps=[])


def scope():
    return {"document_ids": ["D2", "D1"], "audit_watermark": WATERMARK,
        "audit_counts": {"total_documents": 20, "verified_documents": 3, "missing_documents": 17}}


def test_v5_default_model_membership_and_domain_count_shape_exclude_new_evidence():
    assert build.release_models() == {key: value for key, value in build.MODELS.items()
        if key not in {"financial_publication_evidence","financial_daily_publication_evidence"}}
    assert "financial_publication_evidence" not in build.release_models("release_query_v5")
    assert "financial_publication_evidence" in build.release_models("release_query_v6")
    assert "financial_daily_publication_evidence" not in build.release_models("release_query_v7")
    assert "financial_daily_publication_evidence" in build.release_models("release_query_v8_daily")
    with pytest.raises(ValueError, match="query_version"):
        build.release_models("unregistered_v99")


def test_financial_scope_pins_public_policy_original_target_and_explicit_exclusions():
    result = build.financial_scope_descriptor(target(), scope())
    assert result["kind"] == "financial_documents_v1"
    assert result["document_ids"] == ["D1", "D2"]
    assert result["audit_watermark"] == WATERMARK.isoformat()
    assert result["publication_policy_version"] == "financial_public_pit_v1"
    assert result["original_target_manifest_id"] == "T"
    assert result["original_target_count"] == 2
    assert result["original_target_content_hash"] == "a" * 64
    assert result["audit_counts"] == scope()["audit_counts"]
    assert result["audit_counts_source"] == "external_audit_declaration"
    assert result["excluded_domain"] == "prices"
    assert "core_price_volume" in result["excluded_profiles"]
    assert "valuation" in result["excluded_profiles"]
    assert result["allowed_profiles"] == ["fundamentals_sql", "security_identity"]


@pytest.mark.parametrize("damage", ["empty_documents", "duplicate_documents", "naive_watermark",
    "missing_watermark", "negative_count", "false_denominator", "noninteger_count", "larger_seed_than_denominator"])
def test_invalid_or_ambiguous_research_scope_is_rejected(damage):
    value = scope()
    if damage == "empty_documents":
        value["document_ids"] = []
    elif damage == "duplicate_documents":
        value["document_ids"] = ["D1", "D1"]
    elif damage == "naive_watermark":
        value["audit_watermark"] = "2026-10-01"
    elif damage == "missing_watermark":
        del value["audit_watermark"]
    elif damage == "negative_count":
        value["audit_counts"]["total_documents"] = -1
    elif damage == "false_denominator":
        value["audit_counts"]["verified_documents"] = 5
    elif damage == "noninteger_count":
        value["audit_counts"]["missing_documents"] = "17"
    else:
        value["audit_counts"] = {"total_documents": 1, "verified_documents": 1, "missing_documents": 0}
    with pytest.raises(ValueError, match="financial_scope"):
        build.financial_scope_descriptor(target(), value)


def test_publication_evidence_brings_its_bound_document_and_filing_into_closure():
    row = FinancialPublicationEvidence(id="E", document_id="D", filing_id="F")
    assert build.dependencies(row) == [("financial_source_document", "D"), ("filing", "F")]


def test_scope_count_includes_complete_dependency_documents_and_never_claims_declared_verification():
    from test_financial_public_availability import certificate, fixture_document
    first, filing = fixture_document()
    first.id = "D1"
    second, second_filing = fixture_document()
    second.id = "D2"
    second.filing_id = second_filing.id = "F2"
    dependency, dependency_filing = fixture_document()
    dependency.id = "D3"
    dependency.filing_id = dependency_filing.id = "F3"
    proof = certificate(first, filing)
    rows = {domain: {} for domain in build.MODELS}
    rows["financial_source_document"] = {document.id: document for document in (first, second, dependency)}
    rows["filing"] = {item.id: item for item in (filing, second_filing, dependency_filing)}
    rows["financial_publication_evidence"] = {proof.id: proof}
    result = build.financial_scope_coverage(build.financial_scope_descriptor(target(), scope()), rows)
    assert result["included_document_ids"] == ["D1", "D2", "D3"]
    assert result["included_document_count"] == 3
    assert result["excluded_document_count"] == 17
    assert result["included_public_counts"] == {"total_documents": 3, "verified_documents": 1,
        "missing_documents": 2, "withdrawn_documents": 0}
    assert result["audit_counts"]["verified_documents"] == 3


def test_scope_coverage_rejects_missing_seed_or_closure_larger_than_original_denominator():
    rows = {domain: {} for domain in build.MODELS}
    descriptor = build.financial_scope_descriptor(target(), scope())
    with pytest.raises(ValueError, match="financial_scope_document_missing"):
        build.financial_scope_coverage(descriptor, rows)


def test_v6_budget_includes_catalog_securities_issuers_and_observations():
    catalog = {"securities": {"S": {}}, "issuers": {"I": {}}, "observations": [{"id": "O"}]}
    rows = {"financial_source_document": {"D": object()}, "financial_publication_evidence": {"E": object()}}
    assert build.candidate_row_count(rows, catalog) == 5


def test_v5_scope_and_unknown_versions_are_rejected_before_any_database_reads():
    with pytest.raises(ValueError, match="financial_scope_requires_v6"):
        build.collect_candidate(None, target(), financial_scope=scope())
    with pytest.raises(ValueError, match="query_version"):
        build.collect_candidate(None, target(), query_version="future_v99")


def test_financial_only_scope_never_freezes_excluded_price_or_fund_rows():
    rows = {domain: {} for domain in build.MODELS}
    rows["price_observation"]["P"] = object()
    descriptor = build.financial_scope_descriptor(target(), scope())
    with pytest.raises(ValueError, match="financial_scope_excluded_dependency"):
        build.financial_scope_coverage(descriptor, rows)
