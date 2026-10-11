"""Append missing v2 DART amount nodes only after recovering identical source bytes.

Original documents, facts and decisions remain unchanged. Recovered nodes use the
new response capture clock; unarchived API responses are held only in memory.
"""

from contextlib import contextmanager
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
import re
from urllib.parse import urlsplit

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..catalog.models import Issuer
from ..db import transaction, utcnow
from ..ingestion.jobs import stable_hash
from ..releases.models import DatasetRelease, ReleaseSegment, SegmentMember, ValidationIssue
from .adapters.opendart import parse_financial_json
from .models import Filing, FinancialSourceDocument, SourceFinancialFact
from .storage import adopt_extraction, assert_sql_inventory, json_value, source_fact_record, store_extraction


LEGACY_PARSER = "opendart_financial_json_v2"
REPAIR_VERSION = "opendart_v2_missing_node_append_v1"
ENDPOINT = "fnlttSinglAcntAll.json"


@contextmanager
def _session(database, *, write=False):
    if isinstance(database, Session):
        if write:
            with database.begin_nested():
                yield database
        else:
            yield database
    else:
        with transaction(database) as session:
            yield session


def _stored_rows(session, document_id):
    return session.scalars(select(SourceFinancialFact).where(SourceFinancialFact.document_id == document_id)).all()


def _emitted_nodes(rows):
    nodes = []
    for row in rows:
        source_nodes = row.evidence.get("source_node_ids")
        if not isinstance(source_nodes, list) or not source_nodes or any(not isinstance(node, str) for node in source_nodes):
            raise ValueError("legacy_source_node_inventory_invalid")
        nodes.extend(source_nodes)
    if len(set(nodes)) != len(nodes):
        raise ValueError("legacy_source_node_inventory_invalid")
    return nodes


def _has_mismatch(document, rows):
    counts = document.inventory.get("counts", {})
    emitted = len(_emitted_nodes(rows))
    return (counts.get("numeric_nodes") != emitted or counts.get("extracted_nodes") != emitted
            or len(rows) + counts.get("deduplicated_nodes", 0) != emitted)


def _assert_unsealed(session, document, rows):
    members = or_(
        and_(SegmentMember.record_domain == "financial_source_document", SegmentMember.record_id == document.id),
        and_(SegmentMember.record_domain == "source_financial_fact", SegmentMember.record_id.in_([row.id for row in rows])))
    sealed = session.scalar(select(SegmentMember.segment_id)
        .join(ReleaseSegment, ReleaseSegment.segment_id == SegmentMember.segment_id)
        .join(DatasetRelease, DatasetRelease.id == ReleaseSegment.release_id)
        .where(DatasetRelease.status == "sealed", members).limit(1))
    if sealed is not None:
        raise ValueError("sealed_document_cannot_be_repaired")


def _request(document, filing, issuer):
    metadata = document.inventory.get("source_request_metadata", {})
    if (not re.fullmatch(r"DART:\d{8}", issuer.source_identifier)
            or not re.fullmatch(r"\d{14}", str(metadata.get("rcept_no", "")))
            or metadata.get("rcept_no") != filing.accession
            or not re.fullmatch(r"\d{4}", str(metadata.get("bsns_year", "")))
            or metadata.get("reprt_code") not in {"11011", "11012", "11013", "11014"}
            or metadata.get("fs_div") not in {"CFS", "OFS"}
            or document.instance_key != f"structured:{metadata.get('fs_div')}"):
        raise ValueError("legacy_source_request_identity_invalid")
    url = urlsplit(document.source_url)
    if (url.scheme != "https" or url.hostname != "opendart.fss.or.kr" or url.port not in {None, 443}
            or url.username or url.password or url.query or url.fragment or url.path != f"/api/{ENDPOINT}"):
        raise ValueError("legacy_source_endpoint_invalid")
    return {"corp_code": issuer.source_identifier[5:], "bsns_year": metadata["bsns_year"],
            "reprt_code": metadata["reprt_code"], "fs_div": metadata["fs_div"]}


def _document_state(document, filing, issuer):
    return {"content_hash": document.content_hash, "inventory": document.inventory,
            "parser_version": document.parser_version, "available_at": document.available_at,
            "captured_at": document.captured_at, "source_url": document.source_url,
            "instance_key": document.instance_key, "filing_id": filing.id, "issuer_id": issuer.id,
            "accession": filing.accession, "source_identifier": issuer.source_identifier}


def _parse_original(response, *, content_hash, receipt, request, inventory):
    if hashlib.sha256(response.content).hexdigest() != content_hash:
        raise ValueError("original_content_hash_mismatch")
    if (response.status_code != 200 or response.captured_at.tzinfo is None
            or response.captured_at.utcoffset() is None):
        raise ValueError("invalid_recovery_response_capture")
    payload = json.loads(response.content, parse_float=Decimal)
    parsed = parse_financial_json(payload, requested_receipt=receipt, requested_fs_div=request["fs_div"],
                                 requested_corp_code=request["corp_code"], legacy_context_ids=True)
    if any(str(row.get("bsns_year", "")) != request["bsns_year"]
           or str(row.get("reprt_code", "")) != request["reprt_code"] for row in payload.get("list", [])):
        raise ValueError("report_identity_mismatch")
    if (not parsed.complete or not parsed.inventory.numeric_nodes
            or json_value(asdict(parsed.inventory)) != inventory.get("counts")
            or json_value({key: asdict(value) for key, value in parsed.contexts.items()}) != inventory.get("contexts")
            or json_value(parsed.units) != inventory.get("units")):
        raise ValueError("original_extraction_inventory_mismatch")
    return payload, parsed


def _repair_signature(fact, node_id):
    return stable_hash([fact.signature, node_id, REPAIR_VERSION])


def _match_existing(rows, parsed, issuer_id):
    expected = {}
    for fact in parsed.facts:
        if len(fact.source_node_ids) != 1 or fact.source_node_ids[0] in expected:
            raise ValueError("original_source_node_inventory_invalid")
        expected[fact.source_node_ids[0]] = fact
    existing_nodes = _emitted_nodes(rows)
    if any(node not in expected for node in existing_nodes):
        raise ValueError("legacy_source_node_not_in_original_response")
    fields = ("issuer_id", "concept", "account_name", "context_id", "dimensions", "period_kind", "period_start",
              "period_end", "instant", "scope", "unit", "original_unit", "value", "raw_value", "is_nil", "scale", "decimals")
    for row in rows:
        nodes = row.evidence["source_node_ids"]
        if len(nodes) != 1:
            raise ValueError("legacy_source_node_inventory_invalid")
        fact = expected[nodes[0]]
        original = source_fact_record(fact, document_id=row.document_id, issuer_id=issuer_id,
            available_at=row.available_at, captured_at=row.captured_at, extracted_at=row.extracted_at)
        if (any(getattr(row, field) != original[field] for field in fields)
                or any(row.evidence.get(key) != value for key, value in original["evidence"].items())):
            raise ValueError("legacy_source_evidence_mismatch")
        if row.signature != fact.signature:
            if (row.signature != _repair_signature(fact, nodes[0])
                    or row.evidence.get("repair_version") != REPAIR_VERSION
                    or row.evidence.get("original_signature") != fact.signature):
                raise ValueError("legacy_source_signature_mismatch")
    return {node: fact for node, fact in expected.items() if node not in set(existing_nodes)}


def repair_dart_inventory(engine, dart, *, store_v3=False):
    """Repair only mismatched, unsealed v2 documents; Engine or Session is accepted.

    Each document is an atomic append. A repeat performs no HTTP fetch and leaves
    recovered IDs and clocks unchanged. Any changed response bytes fail closed.
    """
    summary = {"repair_version": REPAIR_VERSION, "checked_documents": 0, "mismatched_documents": 0,
               "repaired_documents": 0, "recovered_nodes": 0, "appended_source_facts": 0,
               "still_missing": 0, "fresh_v3_documents": 0, "document_archived": False, "documents": []}
    with _session(engine) as session:
        ids = session.scalars(select(FinancialSourceDocument.id).join(Filing, Filing.id == FinancialSourceDocument.filing_id)
            .where(Filing.source == "opendart", FinancialSourceDocument.parser_version == LEGACY_PARSER)
            .order_by(FinancialSourceDocument.id)).all()
    for document_id in ids:
        with _session(engine) as session:
            document = session.get(FinancialSourceDocument, document_id)
            rows = _stored_rows(session, document_id)
            summary["checked_documents"] += 1
            if not _has_mismatch(document, rows):
                continue
            summary["mismatched_documents"] += 1
            _assert_unsealed(session, document, rows)
            if not document.extraction_complete:
                raise ValueError("legacy_document_not_complete")
            filing = session.get(Filing, document.filing_id)
            issuer = session.get(Issuer, filing.issuer_id)
            request = _request(document, filing, issuer)
            state = _document_state(document, filing, issuer)
        response, _ = dart.fetch(ENDPOINT, request)
        payload, parsed = _parse_original(response, content_hash=state["content_hash"], receipt=state["accession"],
                                          request=request, inventory=state["inventory"])
        with _session(engine, write=True) as session:
            session.execute(select(Filing.id).where(Filing.id == state["filing_id"]).with_for_update())
            document = session.scalar(select(FinancialSourceDocument).where(FinancialSourceDocument.id == document_id)
                .with_for_update().execution_options(populate_existing=True))
            filing = session.get(Filing, document.filing_id)
            issuer = session.get(Issuer, filing.issuer_id)
            if _document_state(document, filing, issuer) != state:
                raise ValueError("legacy_document_changed_during_recovery")
            rows = _stored_rows(session, document_id)
            _assert_unsealed(session, document, rows)
            missing = _match_existing(rows, parsed, issuer.id)
            if not missing:
                assert_sql_inventory(session, document, expected_node_ids=[fact.id for fact in parsed.facts])
                continue
            visibility = max(document.available_at, response.captured_at)
            now = utcnow()
            records = [source_fact_record(fact, document_id=document_id, issuer_id=issuer.id,
                available_at=visibility, captured_at=response.captured_at, extracted_at=now,
                signature=_repair_signature(fact, node_id), evidence={"repair_version": REPAIR_VERSION,
                    "original_signature": fact.signature, "source_row_ordinal": int(node_id.split(":")[1]),
                    "original_document_id": document_id, "response_content_hash": state["content_hash"]})
                for node_id, fact in missing.items()]
            for offset in range(0, len(records), 500):
                session.execute(insert(SourceFinancialFact).values(records[offset:offset + 500])
                    .on_conflict_do_nothing(index_elements=[SourceFinancialFact.document_id, SourceFinancialFact.signature]))
            session.flush()
            assert_sql_inventory(session, document, expected_node_ids=[fact.id for fact in parsed.facts])
            adopt_extraction(session, document)
            session.add(ValidationIssue(record_domain="financial_source_document", record_id=document_id,
                rule=REPAIR_VERSION, severity="info", created_at=now, evidence=json_value({
                    "repair_version": REPAIR_VERSION, "source_parser_version": LEGACY_PARSER,
                    "missing_source_node_ids": sorted(missing), "recovered_nodes": len(missing),
                    "response_content_hash": state["content_hash"], "original_content_hash": state["content_hash"],
                    "response_captured_at": response.captured_at, "recovery_available_at": visibility,
                    "document_archived": False})))
            detail = {"document_id": document_id, "recovered_nodes": len(missing), "fresh_document_id": None}
            if store_v3:
                fresh_parsed = parse_financial_json(payload, requested_receipt=filing.accession,
                    requested_fs_div=request["fs_div"], requested_corp_code=request["corp_code"])
                fresh = store_extraction(session, issuer_id=issuer.id, source="opendart", accession=filing.accession,
                    form=filing.form, source_url=document.source_url, content=response.content, result=fresh_parsed,
                    instance_key=document.instance_key, available_at=visibility, captured_at=response.captured_at,
                    disclosure_profile=document.disclosure_profile, accounting_basis=document.accounting_basis,
                    assurance=document.assurance, source_request_metadata=document.inventory["source_request_metadata"],
                    source_lineage={"kind": "reparsed_identical_original_content", "original_document_id": document_id,
                        "original_parser_version": LEGACY_PARSER, "repair_version": REPAIR_VERSION,
                        "document_archived": False})
                detail["fresh_document_id"] = fresh.id
                summary["fresh_v3_documents"] += 1
            session.flush()
            summary["repaired_documents"] += 1
            summary["recovered_nodes"] += len(missing)
            summary["appended_source_facts"] += len(missing)
            summary["documents"].append(detail)
    return summary
