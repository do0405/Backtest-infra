"""Resumable whole-document publication supplementation, with injected transport.

Cursor ordering and a fixed document capture watermark preserve the audit target.
All facts of a selected document are loaded, including facts outside the watermark;
such documents get an explicit gap rather than a truncated historical certificate.
The caller owns outer transaction commits, provider budgets, and the audit manifest.
"""
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import json
import re
from types import SimpleNamespace

from sqlalchemy import func, select

from ..catalog.models import Issuer
from ..ingestion.models import CollectionJob
from .domain import stable_hash
from .models import Filing, FinancialSourceDocument, SourceFinancialFact
from .publication_models import FinancialPublicationEvidence
from .public_availability import append_publication_evidence, resolve_document_availability
from .public_version_sources import (
    ArchiveLimits, VerificationOutcome, _Gap, _check_identity, _dart_list,
    verify_dart_receipt_original, verify_sec_dated_feed,
)


MAX_BATCH_DOCUMENTS = 200
MAX_FACTS_PER_DOCUMENT = 200000


@dataclass(frozen=True, slots=True)
class PublicationTarget:
    document: object
    filing: object
    issuer: object
    sql_facts: tuple
    job_metadata: tuple = ()
    existing_evidence: tuple = ()
    preflight_reason: str | None = None


@dataclass(frozen=True, slots=True)
class FetchRequest:
    source: str
    url: str
    params: dict = field(default_factory=dict)
    max_bytes: int = 64 * 1024 * 1024


def _aware(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} requires a timezone-aware timestamp")
    return value


def _target_statement(watermark, sources):
    return select(FinancialSourceDocument, Filing, Issuer).join(
        Filing, FinancialSourceDocument.filing_id == Filing.id).join(Issuer, Filing.issuer_id == Issuer.id).where(
        FinancialSourceDocument.captured_at <= watermark, Filing.source.in_(sources))


def publication_target_counts(session, *, watermark, sources=("sec", "opendart")) -> dict:
    """Full denominator at the declared watermark; no completeness filtering."""
    _aware(watermark, "watermark")
    statement = select(Filing.source, func.count(FinancialSourceDocument.id)).join(
        FinancialSourceDocument, FinancialSourceDocument.filing_id == Filing.id).where(
        FinancialSourceDocument.captured_at <= watermark, Filing.source.in_(tuple(sources))).group_by(Filing.source)
    by_source = {source: count for source, count in session.execute(statement)}
    return {"audit_watermark": watermark.isoformat(), "total_documents": sum(by_source.values()),
        "by_source": by_source}


def publication_target_query(*, watermark, after_document_id=None, limit=50, sources=("sec", "opendart"),
        document_ids=None):
    """Expose the bounded, ordered SQL plan for audit/CLI inspection."""
    _aware(watermark, "watermark")
    if type(limit) is not int or not 1 <= limit <= MAX_BATCH_DOCUMENTS:
        raise ValueError("batch limit outside bounded range")
    sources = tuple(sources)
    if not sources or set(sources) - {"sec", "opendart"}:
        raise ValueError("unsupported financial publication sources")
    statement = _target_statement(watermark, sources)
    if document_ids is not None:
        if (not isinstance(document_ids, (list, tuple, set, frozenset)) or not document_ids
                or any(not isinstance(identity, str) or not identity.strip() for identity in document_ids)
                or len(set(document_ids)) != len(document_ids)):
            raise ValueError("frozen document IDs require a nonempty valid unique ID collection")
        statement = statement.where(FinancialSourceDocument.id.in_(sorted(document_ids)))
    if after_document_id is not None:
        if not isinstance(after_document_id, str) or not after_document_id:
            raise ValueError("invalid document cursor")
        statement = statement.where(FinancialSourceDocument.id > after_document_id)
    return statement.order_by(FinancialSourceDocument.id).limit(limit)


def load_publication_batch(session, *, watermark, after_document_id=None, limit=50,
        sources=("sec", "opendart"), max_facts_per_document=MAX_FACTS_PER_DOCUMENT,
        document_ids=None) -> list[PublicationTarget]:
    """Load every fact, or mark the whole document oversized without reading a subset."""
    if type(max_facts_per_document) is not int or max_facts_per_document <= 0:
        raise ValueError("invalid document inventory limit")
    statement = publication_target_query(watermark=watermark, after_document_id=after_document_id,
        limit=limit, sources=sources, document_ids=document_ids)
    targets = []
    for document, filing, issuer in session.execute(statement):
        count = session.scalar(select(func.count(SourceFinancialFact.id)).where(SourceFinancialFact.document_id == document.id))
        reason = "document_inventory_limit" if count > max_facts_per_document else None
        facts = () if reason else tuple(session.scalars(select(SourceFinancialFact).where(
            SourceFinancialFact.document_id == document.id).order_by(SourceFinancialFact.id)).all())
        if not reason and _watermark_incomplete(document, facts, watermark):
            reason = "watermark_incomplete_document"
        jobs = session.scalars(select(CollectionJob).where(CollectionJob.source == filing.source,
            CollectionJob.domain == "filing_sql", CollectionJob.entity_id == filing.accession,
            CollectionJob.request["issuer_id"].astext == filing.issuer_id).order_by(CollectionJob.id)).all()
        evidence = tuple(session.scalars(select(FinancialPublicationEvidence).where(
            FinancialPublicationEvidence.document_id == document.id).order_by(
            FinancialPublicationEvidence.recorded_at, FinancialPublicationEvidence.id)).all())
        targets.append(PublicationTarget(document, filing, issuer, facts,
            tuple(job.request for job in jobs), evidence, reason))
    return targets


def _watermark_incomplete(document, facts, watermark):
    clocks = [getattr(document, "captured_at", None)]
    clocks.extend(getattr(row, field, None) for row in facts for field in ("captured_at", "extracted_at", "first_seen_at"))
    return any(not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None or value > watermark for value in clocks)


def _failure(target, reason, *, requests=()):
    evidence = {"reason": reason}
    if requests:
        evidence["requests"] = [{"source": request.source, "url": request.url, "params": request.params,
            "max_bytes": request.max_bytes} for request in requests]
    return VerificationOutcome("unavailable", reason, None, evidence)


def _matching_metadata(target):
    metadata = []
    for request in target.job_metadata:
        if not isinstance(request, dict) or request.get("issuer_id") != target.filing.issuer_id:
            continue
        filing = request.get("filing")
        if not isinstance(filing, dict):
            continue
        if target.filing.source == "sec":
            if filing.get("accessionNumber") != target.filing.accession or filing.get("form") != target.filing.form:
                continue
        elif (filing.get("rcept_no") != target.filing.accession or filing.get("report_nm") != target.filing.form
                or filing.get("corp_code") != target.issuer.source_identifier.removeprefix("DART:")):
            continue
        metadata.append(request)
    return metadata


def _sec_day(target):
    metadata = _matching_metadata(target)
    days = {request["filing"].get("filingDate") for request in metadata}
    if not days:
        raise _Gap("sec_filing_metadata_missing")
    if len(days) != 1:
        raise _Gap("sec_filing_metadata_ambiguous")
    day = next(iter(days))
    if not isinstance(day, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day):
        raise _Gap("sec_filing_metadata_invalid_date")
    try:
        return date.fromisoformat(day)
    except ValueError as exc:
        raise _Gap("sec_filing_metadata_invalid_date") from exc


def _sec_requests(day, limits):
    quarter = (day.month - 1) // 3 + 1
    suffix = f"{day.year}/QTR{quarter}/{day.strftime('%Y%m%d')}.nc.tar.gz"
    return tuple(FetchRequest("sec", f"https://www.sec.gov/Archives/edgar/{kind}/{suffix}",
        max_bytes=limits.max_compressed_bytes) for kind in ("Feed", "Oldloads"))


def _transport_reason(exc):
    code = getattr(exc, "code", None)
    if isinstance(code, (str, int)) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(code)):
        return "transport:" + str(code)
    return "transport:failed"


class _FetchCache:
    """Retain failures and at most one bounded response; source bytes stay in memory."""
    def __init__(self, fetch, now):
        self.fetch, self.now = fetch, now
        self.failures, self.last_key, self.last_response = {}, None, None

    def request(self, request):
        key = stable_hash((request.source, request.url, request.params, request.max_bytes))
        if key in self.failures:
            raise _Gap(self.failures[key])
        if key == self.last_key:
            return self.last_response
        self.last_key, self.last_response = None, None
        try:
            response = self.fetch(request)
            content = response if isinstance(response, bytes) else response.content
            captured_at = self.now() if isinstance(response, bytes) else response.captured_at
            _aware(captured_at, "transport captured_at")
            if not isinstance(content, bytes):
                raise _Gap("transport:invalid_body")
            if len(content) > request.max_bytes:
                raise _Gap("transport:response_size_exceeded")
            self.last_response = SimpleNamespace(content=content, captured_at=captured_at)
            self.last_key = key
            return self.last_response
        except _Gap as exc:
            self.failures[key] = str(exc)
            raise
        except Exception as exc:
            reason = _transport_reason(exc)
            self.failures[key] = reason
            raise _Gap(reason) from None


def _attempt_sec(target, transport, limits, now):
    try:
        day = _sec_day(target)
        identifier = target.issuer.source_identifier
        if not re.fullmatch(r"CIK[0-9]{1,10}", identifier):
            raise _Gap("sec_issuer_cik_unverified")
        requests = _sec_requests(day, limits)
    except _Gap as exc:
        return _failure(target, str(exc)), now()
    outcomes, captured = [], []
    for request in requests:
        try:
            response = transport.request(request)
            captured.append(response.captured_at)
            outcome = verify_sec_dated_feed(document=target.document, filing=target.filing,
                sql_facts=target.sql_facts, archive_bytes=response.content, archive_url=request.url,
                archive_day=day, expected_cik=identifier.removeprefix("CIK"), limits=limits)
            if outcome.status == "qualified":
                return outcome, max(captured)
            outcomes.append(outcome.reason)
        except _Gap as exc:
            outcomes.append(str(exc))
    reason = outcomes[-1] if len(set(outcomes)) == 1 else "sec_distribution_unverified"
    failure = _failure(target, reason, requests=requests)
    failure.evidence["attempt_reasons"] = outcomes
    return failure, max(captured + [now()])


def _attempt_dart(target, transport, limits, now):
    if str(target.document.parser_version).startswith("opendart_financial_json"):
        return _failure(target, "dart_json_original_mapping_unavailable"), now()
    metadata = _matching_metadata(target)
    if not metadata:
        return _failure(target, "dart_filing_metadata_missing"), now()
    receipt = target.filing.accession
    corp_code = target.issuer.source_identifier.removeprefix("DART:")
    if not re.fullmatch(r"[0-9]{14}", receipt) or not re.fullmatch(r"[0-9]{8}", corp_code):
        return _failure(target, "dart_receipt_or_company_unverified"), now()
    days = {request["filing"].get("rcept_dt") for request in metadata}
    if days != {receipt[:8]}:
        return _failure(target, "dart_filing_metadata_date_mismatch"), now()
    source_request = target.document.inventory.get("source_request_metadata", {})
    codes = {source_request.get("reprt_code")} - {None}
    if not codes:
        codes = {request.get("reprt_code") for request in metadata} - {None}
    if not codes:
        codes = {code for request in metadata for code in request.get("reprt_code_candidates", [])}
    if not codes or codes - {"11011", "11012", "11013", "11014"}:
        return _failure(target, "dart_original_report_code_unverified"), now()
    requests, captured = [], []
    try:
        list_response = None
        for page in range(1, 101):
            request = FetchRequest("opendart", "https://opendart.fss.or.kr/api/list.json", params={"corp_code": corp_code,
                "bgn_de": receipt[:8], "end_de": receipt[:8], "last_reprt_at": "N", "page_count": 100, "page_no": page},
                max_bytes=limits.max_public_list_bytes)
            requests.append(request)
            response = transport.request(request)
            captured.append(response.captured_at)
            try:
                _dart_list(response.content, target.filing, corp_code, receipt, limits)
                list_response = response
                break
            except _Gap as exc:
                if str(exc) != "dart_public_list:receipt_missing_or_ambiguous":
                    raise
                try:
                    payload = json.loads(response.content)
                    total_pages = int(payload.get("total_page", 1))
                except (ValueError, TypeError, AttributeError):
                    raise _Gap("dart_public_list:invalid_pagination") from None
                if page >= total_pages:
                    raise
        if list_response is None:
            raise _Gap("dart_public_list:page_limit")
        outcomes = []
        for code in sorted(codes):
            request = FetchRequest("opendart", "https://opendart.fss.or.kr/api/fnlttXbrl.xml",
                params={"rcept_no": receipt, "reprt_code": code}, max_bytes=limits.max_compressed_bytes)
            requests.append(request)
            try:
                response = transport.request(request)
                captured.append(response.captured_at)
                outcome = verify_dart_receipt_original(document=target.document, filing=target.filing, sql_facts=target.sql_facts,
                    archive_bytes=response.content, original_url=request.url, public_list_body=list_response.content,
                    public_list_url="https://opendart.fss.or.kr/api/list.json", expected_corp_code=corp_code,
                    requested_receipt=receipt, limits=limits)
                if outcome.status == "qualified":
                    return outcome, max(captured)
                outcomes.append(outcome.reason)
            except _Gap as exc:
                outcomes.append(str(exc))
        reason = outcomes[-1] if len(set(outcomes)) == 1 else "dart_original_unverified"
        failure = _failure(target, reason, requests=requests)
        failure.evidence["attempt_reasons"] = outcomes
        return failure, max(captured + [now()])
    except _Gap as exc:
        return _failure(target, str(exc), requests=requests), max(captured + [now()])


def run_publication_batch(session, *, targets, watermark, fetch, now=None, append=None,
        limits=None, original_target_count=None) -> dict:
    """Append independent document outcomes; return a safe contiguous resume cursor.

    fetch(FetchRequest) must use the existing provider/account rate budget. It may
    return bytes or ProviderResponse. No source response bytes enter SQL evidence.
    SQL failures stop cursor advancement while later documents can still finish.
    """
    _aware(watermark, "watermark")
    targets = list(targets)
    identifiers = [target.document.id for target in targets]
    if identifiers != sorted(set(identifiers)):
        raise ValueError("batch targets require ordered unique document IDs")
    if len(targets) > MAX_BATCH_DOCUMENTS:
        raise ValueError("batch limit outside bounded range")
    if original_target_count is not None and (type(original_target_count) is not int or original_target_count < len(targets)):
        raise ValueError("original target denominator cannot shrink below the page")
    now = now or (lambda: datetime.now(timezone.utc))
    append = append or append_publication_evidence
    limits = limits or ArchiveLimits()
    transport = _FetchCache(fetch, now)
    result = {"audit_watermark": watermark.isoformat(), "original_target_count": original_target_count,
        "processed_count": len(targets), "qualified_count": 0, "unavailable_count": 0,
        "already_qualified_count": 0, "sql_failed_count": 0, "next_cursor": None,
        "resume_blocked_document_ids": [], "results": []}
    blocked = False
    for target in targets:
        captured_at = _aware(now(), "now")
        preflight = target.preflight_reason
        if not preflight and getattr(target.issuer, "id", None) != target.filing.issuer_id:
            preflight = "issuer_binding_mismatch"
        if not preflight and _watermark_incomplete(target.document, target.sql_facts, watermark):
            preflight = "watermark_incomplete_document"
        if not preflight and target.existing_evidence:
            resolved = resolve_document_availability(target.document, target.filing, target.existing_evidence,
                history_mode="reconstructed_public", as_of=captured_at)
            if resolved["status"] == "qualified":
                result["already_qualified_count"] += 1
                result["results"].append({"document_id": target.document.id, "status": "already_qualified",
                    "reason": None, "evidence_ids": resolved["evidence_ids"]})
                if not blocked:
                    result["next_cursor"] = target.document.id
                continue
        if preflight:
            outcome = _failure(target, preflight)
        elif target.filing.source == "opendart" and str(target.document.parser_version).startswith("opendart_financial_json"):
            outcome = _failure(target, "dart_json_original_mapping_unavailable")
        else:
            try:
                _check_identity(target.document, target.filing, target.sql_facts, target.filing.source)
            except _Gap as exc:
                outcome = _failure(target, str(exc))
            except (AttributeError, TypeError, ValueError, OverflowError):
                outcome = _failure(target, "publication_metadata_invalid")
            else:
                try:
                    if target.filing.source == "sec":
                        outcome, captured_at = _attempt_sec(target, transport, limits, now)
                    elif target.filing.source == "opendart":
                        outcome, captured_at = _attempt_dart(target, transport, limits, now)
                    else:
                        outcome = _failure(target, "unsupported_publication_source")
                except (AttributeError, TypeError, ValueError, OverflowError):
                    outcome = _failure(target, "publication_metadata_invalid")
        evidence = {**outcome.evidence, "batch": {"audit_watermark": watermark.isoformat()}}
        try:
            with session.begin_nested():
                row = append(session, document=target.document, filing=target.filing, status=outcome.status,
                    evidence=evidence, public_day=outcome.public_day, captured_at=captured_at,
                    supersedes_assertion_hash=None)
            result[outcome.status + "_count"] += 1
            item = {"document_id": target.document.id, "status": outcome.status, "reason": outcome.reason,
                "evidence_ids": [row.id]}
        except Exception:
            blocked = True
            result["sql_failed_count"] += 1
            result["resume_blocked_document_ids"].append(target.document.id)
            item = {"document_id": target.document.id, "status": "sql_failed", "reason": "sql_append_failed", "evidence_ids": []}
        result["results"].append(item)
        if not blocked:
            result["next_cursor"] = target.document.id
    result["reason_counts"] = dict(sorted(Counter(item["reason"] for item in result["results"] if item["reason"]).items()))
    return result
