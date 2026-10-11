"""Transactional SQL extraction gate; financial documents are never archived."""
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
import hashlib

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ..db import new_id, utcnow
from ..catalog.models import Issuer, CatalogObservation
from ..ingestion.jobs import stable_hash
from ..releases.models import SelectionDecision
from .domain import Context
from .models import Filing, FinancialSourceDocument, SourceFinancialFact


FINANCIAL_WRITE_BATCH_SIZE = 50


def _fence(session, fence):
    if fence is not None:
        # A lease UPDATE must not first flush the very rows it protects.
        with session.no_autoflush:
            fence(session)


def _flush(session, fence):
    _fence(session, fence)
    session.flush()
    _fence(session, fence)


def _execute(session, statement, fence):
    _fence(session, fence)
    with session.no_autoflush:
        result = session.execute(statement)
    _fence(session, fence)
    return result


def _scalar(session, statement, fence):
    _fence(session, fence)
    with session.no_autoflush:
        result = session.scalar(statement)
    _fence(session, fence)
    return result


def _read_batches(session, statement, fence):
    """Validate ownership around each fetch; a blocked SQL call can still expire."""
    _fence(session, fence)
    with session.no_autoflush:
        result = session.scalars(statement.execution_options(yield_per=FINANCIAL_WRITE_BATCH_SIZE))
        try:
            _fence(session, fence)
            partitions = iter(result.partitions(FINANCIAL_WRITE_BATCH_SIZE))
            while True:
                _fence(session, fence)
                batch = next(partitions, None)
                _fence(session, fence)
                if batch is None:
                    break
                yield batch
                _fence(session, fence)
        finally:
            result.close()


def json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    return value


def _context_inventory(result):
    """Reported contexts plus clearly marked unknown references retained for audit."""
    contexts = {key: asdict(value) for key, value in result.contexts.items()}
    documented_missing = {issue.node_id for issue in result.issues if issue.code == "missing_context"}
    for fact in result.facts:
        if fact.context_id in result.contexts:
            continue
        if fact.parse_status != "missing_context" or not set(fact.source_node_ids).issubset(documented_missing):
            continue
        if fact.context_id not in contexts:
            contexts[fact.context_id] = {**asdict(Context(fact.context_id)), "reported": False,
                "resolution_status": "missing_context", "source_node_ids": []}
        contexts[fact.context_id]["source_node_ids"].extend(fact.source_node_ids)
    return contexts


def store_extraction(session, *, issuer_id, source, accession, form, source_url, content, result,
                     available_at, captured_at, published_at=None, disclosure_profile="periodic_report",
                     accounting_basis="unknown", assurance="unknown", instance_key="", source_request_metadata=None,
                     source_lineage=None, fence=None):
    """Roll back this extraction if SQL loses any declared source nodes."""
    # begin_nested flushes unconditionally, even inside no_autoflush.
    _fence(session, fence)
    with session.begin_nested(), session.no_autoflush:
        _fence(session, fence)
        document = _store_extraction(session, issuer_id=issuer_id, source=source, accession=accession, form=form,
            source_url=source_url, content=content, result=result, available_at=available_at, captured_at=captured_at,
            published_at=published_at, disclosure_profile=disclosure_profile, accounting_basis=accounting_basis,
            assurance=assurance, instance_key=instance_key, source_request_metadata=source_request_metadata,
            source_lineage=source_lineage, fence=fence)
        _flush(session, fence)
    _fence(session, fence)
    return document


def _store_extraction(session, *, issuer_id, source, accession, form, source_url, content, result,
                      available_at, captured_at, published_at=None, disclosure_profile="periodic_report",
                      accounting_basis="unknown", assurance="unknown", instance_key="", source_request_metadata=None,
                      source_lineage=None, fence=None):
    """Persist lexical/numeric/context evidence atomically; repeat does not reset clocks.

    Changed content of an existing accession uses capture availability unless an
    independently verified revision publication timestamp is supplied separately.
    An incomplete extraction is retained but not eligible for accepted financial queries.
    """
    if "?" in source_url:
        raise ValueError("Financial provenance URL must not contain credential query parameters")
    if source_request_metadata and set(source_request_metadata) - {"rcept_no", "reprt_code", "bsns_year", "fs_div", "report_code_certification"}:
        raise ValueError("Unregistered financial request metadata")
    for timestamp in (available_at, captured_at):
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Timezone-aware source clocks required")
    filing = _scalar(session, select(Filing).where(Filing.source == source, Filing.accession == accession, Filing.issuer_id == issuer_id), fence)
    if filing is None:
        filing = Filing(issuer_id=issuer_id, source=source, accession=accession, form=form, published_at=published_at,
                        available_at=available_at, captured_at=captured_at, evidence={"original_publication_verified": published_at is not None})
        session.add(filing)
        _flush(session, fence)
    digest = hashlib.sha256(content).hexdigest()
    _execute(session, select(Filing.id).where(Filing.id == filing.id).with_for_update(), fence)
    previous = [document for batch in _read_batches(session,
        select(FinancialSourceDocument).where(FinancialSourceDocument.filing_id == filing.id,
            FinancialSourceDocument.instance_key == instance_key), fence) for document in batch]
    same = next((item for item in previous if item.content_hash == digest and item.parser_version == result.parser_version), None)
    if same:
        _record_url(session, same, source_url, captured_at, fence=fence)
        adopt_extraction(session, same,
            expected_node_ids=[node_id for fact in result.facts for node_id in fact.source_node_ids], fence=fence)
        return same
    replacement = any(item.content_hash != digest for item in previous)
    visibility = max(available_at, captured_at) if replacement else available_at
    inventory = {"counts": asdict(result.inventory), "contexts": _context_inventory(result),
                 "units": result.units, "issues": [asdict(issue) for issue in result.issues],
                 "correction_time_unknown": replacement, "source_format_complete": result.complete}
    _fence(session, fence)
    issuer = session.get(Issuer, issuer_id)
    _fence(session, fence)
    identity = {"status": "unverified", "expected": issuer.source_identifier}
    if source == "sec" and issuer.source_identifier.startswith("CIK") and issuer.source_identifier[3:].isdigit():
        expected = int(issuer.source_identifier[3:])
        identifiers = [context.entity_identifier for context in result.contexts.values()
                       if context.entity_scheme and context.entity_scheme.rstrip("/").endswith("/CIK")]
        identity["parsed_identifiers"] = sorted(set(identifiers), key=str)
        if identifiers:
            identity["status"] = "matched" if all(value and value.isdigit() and int(value) == expected for value in identifiers) else "mismatch"
    if source == "opendart" and issuer.source_identifier.startswith("DART:"):
        identifiers = [context.entity_identifier for context in result.contexts.values()
                       if context.entity_scheme == "opendart.corp_code"]
        identity["parsed_identifiers"] = sorted(set(identifiers), key=str)
        if identifiers:
            identity["status"] = "matched" if all(value == issuer.source_identifier[5:] for value in identifiers) else "mismatch"
    inventory["issuer_identity"] = identity
    inventory["source_request_metadata"] = source_request_metadata or {}
    if source_lineage is not None:
        inventory["source_lineage"] = source_lineage
    document = FinancialSourceDocument(filing_id=filing.id, content_hash=digest, source_url=source_url,
        instance_key=instance_key, revision_kind="replacement" if replacement else "original", disclosure_profile=disclosure_profile,
        accounting_basis=accounting_basis, assurance=assurance, available_at=visibility, captured_at=captured_at,
        extraction_complete=result.complete and result.inventory.numeric_nodes > 0 and identity["status"] != "mismatch",
        inventory=json_value(inventory), parser_version=result.parser_version)
    session.add(document)
    _flush(session, fence)
    _record_url(session, document, source_url, captured_at, fence=fence)
    extracted_at = utcnow()
    for offset in range(0, len(result.facts), FINANCIAL_WRITE_BATCH_SIZE):
        _fence(session, fence)
        records = [source_fact_record(fact, document_id=document.id, issuer_id=issuer_id,
            available_at=visibility, captured_at=captured_at, extracted_at=extracted_at)
            for fact in result.facts[offset:offset + FINANCIAL_WRITE_BATCH_SIZE]]
        _execute(session, insert(SourceFinancialFact).values(records)
            .on_conflict_do_nothing(index_elements=[SourceFinancialFact.document_id, SourceFinancialFact.signature]), fence)
    _flush(session, fence)
    adopt_extraction(session, document,
        expected_node_ids=[node_id for fact in result.facts for node_id in fact.source_node_ids], fence=fence)
    return document


def source_fact_record(fact, *, document_id, issuer_id, available_at, captured_at, extracted_at,
                       signature=None, evidence=None):
    """One append-only SQL row, also used for hash-bound legacy node recovery."""
    return {"id": new_id(), "document_id": document_id, "issuer_id": issuer_id,
            "concept": fact.concept, "account_name": fact.account_name or fact.concept, "context_id": fact.context_id,
            "dimensions": json_value(fact.dimensions), "period_kind": fact.period_kind,
            "period_start": fact.period_start, "period_end": fact.period_end,
            "instant": fact.period_end if fact.period_kind == "instant" else None,
            "scope": fact.scope, "unit": fact.unit or "unknown", "original_unit": fact.original_unit or "unknown",
            "value": fact.value, "raw_value": fact.raw_value, "is_nil": fact.is_nil,
            "scale": fact.scale, "decimals": fact.decimals, "available_at": available_at,
            "captured_at": captured_at, "extracted_at": extracted_at, "first_seen_at": extracted_at,
            "signature": signature or fact.signature, "evidence": json_value({**fact.evidence, **(evidence or {}), "taxonomy": fact.taxonomy,
            "precision": fact.precision, "sign": fact.sign, "format": fact.format, "parse_status": fact.parse_status,
            "source_node_ids": fact.source_node_ids, "raw_numeric_node": fact.raw_xml})}


def assert_sql_inventory(session, document, *, expected_node_ids=None, fence=None):
    """Check emitted nodes after INSERT, including raw nodes behind deduplicated facts."""
    _flush(session, fence)
    counts = document.inventory.get("counts", {})
    rows, nodes = [], []
    for batch in _read_batches(session, select(SourceFinancialFact).where(SourceFinancialFact.document_id == document.id), fence):
        for row in batch:
            source_nodes = row.evidence.get("source_node_ids")
            if not isinstance(source_nodes, list) or not source_nodes or any(not isinstance(node, str) for node in source_nodes):
                raise ValueError("SQL extraction inventory mismatch: invalid source node IDs")
            nodes.extend(source_nodes)
        rows.extend(batch)
    extracted = counts.get("extracted_nodes")
    if (extracted != len(nodes) or len(set(nodes)) != len(nodes)
            or len(rows) + counts.get("deduplicated_nodes", 0) != extracted
            or counts.get("numeric_nodes") != len(nodes)
            or (expected_node_ids is not None and sorted(expected_node_ids) != sorted(nodes))):
        raise ValueError("SQL extraction inventory mismatch: declared raw nodes differ from stored SQL nodes")
    contexts = document.inventory.get("contexts", {})
    reported_contexts = {key for key, context in contexts.items() if context.get("reported", True) is not False}
    if (set(counts.get("context_ids", [])) != reported_contexts
            or any(row.context_id not in contexts for row in rows)):
        raise ValueError("SQL extraction inventory mismatch: context inventory differs from stored SQL nodes")
    missing_issues = {issue.get("node_id") for issue in document.inventory.get("issues", [])
                      if issue.get("code") == "missing_context"}
    for key in set(contexts) - reported_contexts:
        _fence(session, fence)
        placeholder = contexts[key]
        unresolved_rows = [row for row in rows if row.context_id == key]
        unresolved_nodes = [node for row in unresolved_rows for node in row.evidence["source_node_ids"]]
        unknown = asdict(Context(key))
        if (document.extraction_complete or not unresolved_rows
                or placeholder.get("resolution_status") != "missing_context"
                or any(placeholder.get(field) != unknown[field] for field in unknown)
                or sorted(placeholder.get("source_node_ids", [])) != sorted(unresolved_nodes)
                or not set(unresolved_nodes).issubset(missing_issues)
                or any(row.evidence.get("parse_status") != "missing_context" for row in unresolved_rows)):
            raise ValueError("SQL extraction inventory mismatch: unresolved context lacks documented source-node failure")
    _fence(session, fence)
    return rows


def _record_url(session, document, source_url, captured_at, *, fence=None):
    fields = {"document_id": document.id, "source_url": source_url, "content_hash": document.content_hash,
              "parser_version": document.parser_version, "instance_key": document.instance_key}
    _execute(session, insert(CatalogObservation).values(id=new_id(), source="financial_provenance", identifier=document.id,
        content_hash=stable_hash(fields), fields=fields, captured_at=captured_at).on_conflict_do_nothing(
            index_elements=[CatalogObservation.source, CatalogObservation.identifier, CatalogObservation.content_hash]), fence)


def adopt_extraction(session, document, *, expected_node_ids=None, fence=None):
    """Append current format-only decisions; never approve canonical semantics."""
    _flush(session, fence)
    _execute(session, select(FinancialSourceDocument.id).where(FinancialSourceDocument.id == document.id).with_for_update(), fence)
    assert_sql_inventory(session, document, expected_node_ids=expected_node_ids, fence=fence)
    if not document.extraction_complete:
        existing = _scalar(session, select(SelectionDecision.id).where(SelectionDecision.record_domain == "financial_source_document",
            SelectionDecision.record_id == document.id, SelectionDecision.rule_version == "sql_extraction_inventory_v1"), fence)
        if not existing:
            session.add(SelectionDecision(record_domain="financial_source_document", record_id=document.id, status="quarantined",
                rule_version="sql_extraction_inventory_v1", evidence_grade="incomplete", reason="Incomplete source format inventory retained for audit",
                actor="extraction_gate", recorded_at=utcnow()))
        _flush(session, fence)
        return
    now = utcnow()
    for ids in _read_batches(session, select(SourceFinancialFact.id).where(SourceFinancialFact.document_id == document.id), fence):
        decided = {record_id for batch in _read_batches(session, select(SelectionDecision.record_id).where(
            SelectionDecision.record_domain == "source_financial_fact", SelectionDecision.record_id.in_(ids),
            SelectionDecision.rule_version == "sql_extraction_inventory_v1"), fence) for record_id in batch}
        rows = [{"id": new_id(), "record_domain": "source_financial_fact", "record_id": record_id, "status": "accepted",
            "rule_version": "sql_extraction_inventory_v1", "evidence_grade": "structural_only",
            "reason": "SQL numeric/context inventory matches supported format; semantic mapping and historical issuer identity remain unqualified",
            "actor": "extraction_gate", "recorded_at": now} for record_id in ids if record_id not in decided]
        if rows:
            _execute(session, insert(SelectionDecision).values(rows), fence)
    _flush(session, fence)
