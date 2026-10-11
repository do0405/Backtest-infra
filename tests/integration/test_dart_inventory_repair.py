from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from backtest_data.catalog.models import Issuer
from backtest_data.fundamentals.adapters.opendart import parse_financial_json
from backtest_data.fundamentals.models import Filing, FinancialSourceDocument, SourceFinancialFact
from backtest_data.fundamentals.storage import json_value, store_extraction
from backtest_data.ingestion.http import ProviderResponse
from backtest_data.releases.models import (DataSegment, DatasetRelease, ReleaseSegment, SegmentMember,
                                         SelectionDecision, ValidationIssue)


pytestmark = pytest.mark.integration
OLD = datetime(2020, 1, 1, tzinfo=timezone.utc)
CAPTURE = datetime(2026, 10, 8, tzinfo=timezone.utc)
RECEIPT = "20260320000123"
META = {"rcept_no": RECEIPT, "reprt_code": "11011", "bsns_year": "2025", "fs_div": "CFS"}


def source_payload():
    row = {"rcept_no": RECEIPT, "corp_code": "00123456", "bsns_year": "2025",
           "reprt_code": "11011", "sj_div": "IS", "account_id": "-표준계정코드 미사용-",
           "account_nm": "First anonymous account", "currency": "KRW", "thstrm_amount": "100"}
    return {"status": "000", "list": [row, {**row, "account_nm": "Second anonymous account"}]}


def parse(payload, *, legacy=True):
    return parse_financial_json(payload, requested_receipt=RECEIPT, requested_corp_code="00123456",
                                requested_fs_div="CFS", legacy_context_ids=legacy)


def fact_record(document, issuer, fact):
    return dict(id=f"legacy-node-{fact.id}", document_id=document.id, issuer_id=issuer.id,
        concept=fact.concept, account_name=fact.account_name, context_id=fact.context_id,
        dimensions=fact.dimensions, period_kind=fact.period_kind, period_start=fact.period_start,
        period_end=fact.period_end, instant=None, scope=fact.scope, unit=fact.unit, original_unit=fact.original_unit,
        value=fact.value, raw_value=fact.raw_value, is_nil=fact.is_nil, scale=fact.scale, decimals=fact.decimals,
        available_at=OLD, captured_at=OLD, extracted_at=OLD, first_seen_at=OLD, signature=fact.signature,
        evidence={**fact.evidence, "taxonomy": fact.taxonomy, "precision": fact.precision, "sign": fact.sign,
                  "format": fact.format, "parse_status": fact.parse_status,
                  "source_node_ids": list(fact.source_node_ids), "raw_numeric_node": fact.raw_xml})


def legacy_fixture(session, payload=None):
    payload = payload or source_payload()
    content = json.dumps(payload).encode()
    parsed = parse(payload)
    issuer = Issuer(market="KR", source_identifier="DART:00123456", name="Repair fixture")
    session.add(issuer)
    session.flush()
    filing = Filing(issuer_id=issuer.id, source="opendart", accession=RECEIPT, form="사업보고서 (2025.12)",
                    available_at=OLD, captured_at=OLD, evidence={})
    session.add(filing)
    session.flush()
    inventory = {"counts": asdict(parsed.inventory), "contexts": {key: asdict(value) for key, value in parsed.contexts.items()},
                 "units": {}, "issues": [], "source_format_complete": True, "correction_time_unknown": False,
                 "source_request_metadata": META,
                 "issuer_identity": {"status": "matched", "expected": issuer.source_identifier,
                                     "parsed_identifiers": ["00123456"]}}
    document = FinancialSourceDocument(filing_id=filing.id, content_hash=hashlib.sha256(content).hexdigest(),
        source_url="https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json", instance_key="structured:CFS",
        available_at=OLD, captured_at=OLD, extraction_complete=True, inventory=json_value(inventory),
        parser_version=parsed.parser_version)
    session.add(document)
    session.flush()
    # Reproduce the former INSERT gate: it trusted unique signatures and silently lost one raw node.
    session.execute(insert(SourceFinancialFact).values([fact_record(document, issuer, fact) for fact in parsed.facts])
        .on_conflict_do_nothing(index_elements=[SourceFinancialFact.document_id, SourceFinancialFact.signature]))
    fact = session.scalar(select(SourceFinancialFact).where(SourceFinancialFact.document_id == document.id))
    decision = SelectionDecision(record_domain="source_financial_fact", record_id=fact.id, status="accepted",
        rule_version="sql_extraction_inventory_v1", evidence_grade="structural_only", reason="Legacy fixture",
        actor="extraction_gate", recorded_at=OLD)
    session.add(decision)
    session.flush()
    return document, issuer, fact, decision, content, payload


def snapshot(row):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


def client(content, payload, calls):
    def fetch(endpoint, params):
        calls.append((endpoint, params))
        return ProviderResponse(content, 200, CAPTURE, "application/json"), payload
    return SimpleNamespace(base="https://opendart.fss.or.kr/api/", fetch=fetch)


def test_storage_rejects_legacy_node_loss_and_rolls_back_before_adoption(db_session):
    issuer = Issuer(market="KR", source_identifier="DART:00123456", name="New fixture")
    db_session.add(issuer)
    db_session.flush()
    payload = source_payload()
    with pytest.raises(ValueError, match="SQL extraction inventory mismatch"):
        store_extraction(db_session, issuer_id=issuer.id, source="opendart", accession=RECEIPT,
            form="사업보고서 (2025.12)", source_url="https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json",
            content=json.dumps(payload).encode(), result=parse(payload), available_at=CAPTURE, captured_at=CAPTURE,
            instance_key="structured:CFS", source_request_metadata=META)
    assert db_session.scalar(select(func.count()).select_from(FinancialSourceDocument)) == 0
    assert db_session.scalar(select(func.count()).select_from(SourceFinancialFact)) == 0
    assert db_session.scalar(select(func.count()).select_from(SelectionDecision)) == 0


def test_repair_appends_missing_node_without_changing_old_rows_or_clocks_and_is_idempotent(db_session):
    from backtest_data.fundamentals.dart_inventory_repair import repair_dart_inventory
    document, issuer, old, decision, content, payload = legacy_fixture(db_session)
    assert db_session.scalar(select(func.count()).select_from(SourceFinancialFact)) == 1
    before = [snapshot(row) for row in (document, old, decision)]
    calls = []
    first = repair_dart_inventory(db_session, dart=client(content, payload, calls), store_v3=False)
    rows = db_session.scalars(select(SourceFinancialFact).where(SourceFinancialFact.document_id == document.id)).all()
    assert first["repaired_documents"] == first["appended_source_facts"] == 1
    assert [snapshot(row) for row in (document, old, decision)] == before
    assert len(rows) == 2
    added = next(row for row in rows if row.id != old.id)
    assert added.available_at == added.captured_at == CAPTURE
    assert added.first_seen_at == added.extracted_at and added.first_seen_at >= CAPTURE
    assert added.value == old.value and added.context_id == old.context_id
    assert added.signature != old.signature and added.evidence["original_signature"] == old.signature
    assert added.evidence["source_node_ids"] == ["row:1:thstrm_amount"]
    assert calls == [("fnlttSinglAcntAll.json", {"corp_code": "00123456", "bsns_year": "2025", "reprt_code": "11011", "fs_div": "CFS"})]
    decisions = db_session.scalars(select(SelectionDecision)).all()
    assert len(decisions) == 2 and decision.recorded_at == OLD
    issues = db_session.scalars(select(ValidationIssue)).all()
    assert len(issues) == 1 and issues[0].severity == "info"
    assert issues[0].evidence["missing_source_node_ids"] == ["row:1:thstrm_amount"]
    assert issues[0].evidence["response_content_hash"] == document.content_hash
    after = [snapshot(row) for row in rows + decisions + issues]
    second = repair_dart_inventory(db_session, dart=client(content, payload, calls), store_v3=False)
    assert second["repaired_documents"] == second["appended_source_facts"] == 0
    assert [snapshot(row) for row in rows + decisions + issues] == after and len(calls) == 1


def test_repair_requires_original_content_hash_and_has_no_partial_append(db_session):
    from backtest_data.fundamentals.dart_inventory_repair import repair_dart_inventory
    document, issuer, old, decision, content, payload = legacy_fixture(db_session)
    with pytest.raises(ValueError, match="original_content_hash_mismatch"):
        repair_dart_inventory(db_session, dart=client(content + b" ", payload, []))
    assert db_session.scalar(select(func.count()).select_from(SourceFinancialFact)) == 1
    assert db_session.scalar(select(func.count()).select_from(ValidationIssue)) == 0


@pytest.mark.parametrize("field,value", [("bsns_year", "2024"), ("reprt_code", "11013")])
def test_matching_bytes_cannot_override_the_original_year_and_report_request(db_session, field, value):
    from backtest_data.fundamentals.dart_inventory_repair import repair_dart_inventory
    payload = source_payload()
    for row in payload["list"]:
        row[field] = value
    document, issuer, old, decision, content, payload = legacy_fixture(db_session, payload)
    with pytest.raises(ValueError, match="report_identity_mismatch"):
        repair_dart_inventory(db_session, dart=client(content, payload, []))
    assert db_session.scalar(select(func.count()).select_from(SourceFinancialFact)) == 1
    assert db_session.scalar(select(func.count()).select_from(ValidationIssue)) == 0


def test_recovery_values_are_bound_to_hashed_bytes_and_ignore_unbound_client_payload(db_session):
    from backtest_data.fundamentals.dart_inventory_repair import repair_dart_inventory
    document, issuer, old, decision, content, payload = legacy_fixture(db_session)
    unbound = source_payload()
    unbound["list"][1]["thstrm_amount"] = "999"
    summary = repair_dart_inventory(db_session, dart=client(content, unbound, []))
    rows = db_session.scalars(select(SourceFinancialFact).where(SourceFinancialFact.document_id == document.id)).all()
    assert summary["recovered_nodes"] == 1 and {row.value for row in rows} == {old.value}


def test_repair_refuses_any_document_already_in_a_sealed_release(db_session):
    from backtest_data.fundamentals.dart_inventory_repair import repair_dart_inventory
    document, issuer, old, decision, content, payload = legacy_fixture(db_session)
    segment = DataSegment(domain="financial_source_document", partition_key="fixture", content_hash="a" * 64)
    release = DatasetRelease(manifest={}, content_hash="b" * 64, status="building")
    db_session.add_all([segment, release])
    db_session.flush()
    db_session.add(SegmentMember(segment_id=segment.id, record_domain="financial_source_document", record_id=document.id))
    db_session.flush()
    db_session.add(ReleaseSegment(release_id=release.id, segment_id=segment.id))
    db_session.flush()
    release.status = "sealed"
    db_session.flush()
    calls = []
    with pytest.raises(ValueError, match="sealed_document_cannot_be_repaired"):
        repair_dart_inventory(db_session, dart=client(content, payload, calls))
    assert not calls


def test_repair_can_store_a_fresh_v3_document_with_explicit_same_content_lineage(db_session):
    from backtest_data.fundamentals.dart_inventory_repair import repair_dart_inventory
    original, issuer, old, decision, content, payload = legacy_fixture(db_session)
    summary = repair_dart_inventory(db_session, dart=client(content, payload, []), store_v3=True)
    fresh = db_session.scalar(select(FinancialSourceDocument).where(FinancialSourceDocument.parser_version == "opendart_financial_json_v3"))
    assert summary["fresh_v3_documents"] == 1
    assert fresh.content_hash == original.content_hash and fresh.available_at == fresh.captured_at == CAPTURE
    assert fresh.inventory["source_lineage"]["original_document_id"] == original.id
    assert fresh.inventory["source_lineage"]["document_archived"] is False
    assert len(db_session.scalars(select(SourceFinancialFact).where(SourceFinancialFact.document_id == fresh.id)).all()) == 2
