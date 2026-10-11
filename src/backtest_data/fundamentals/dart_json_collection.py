"""Supplement original XBRL with receipt-verified CFS/OFS structured accounts.

Report titles supply exploratory API request years, never economic dates. The
response must independently match corporation, receipt, year and report code.
Missing fiscal dates remain unknown; no calendar year is silently substituted.
"""
import re

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ..catalog.models import Issuer
from ..db import new_id, transaction
from ..ingestion.http import ProviderError
from ..ingestion.jobs import logical_request, stable_hash
from ..ingestion.models import CollectionJob
from .adapters.opendart import ReceiptMismatch, parse_financial_json
from .collection import dart_report_code
from .models import Filing
from .storage import _fence, store_extraction


def plan_dart_json(session):
    """Plan exact receipt-scoped accounts before or after original XBRL storage.

    Discovery queue metadata is sufficient to make an exploratory request,
    never to certify economic dates or a latest-only response's old receipt.
    Merge all input identities before writing so conflicting receipt/issuer or
    title assertions cannot be resolved by row order. Existing work is reused
    without changing its status, checkpoint, or original XBRL counterpart.
    """
    issuers = {identity: (market, identifier) for identity, market, identifier in session.execute(
        select(Issuer.id, Issuer.market, Issuer.source_identifier))}
    existing = set(session.scalars(select(CollectionJob.identity_hash).where(
        CollectionJob.source == "opendart", CollectionJob.domain == "filing_json")).yield_per(2000))
    stats = {"scanned_filing_records": 0, "scanned_discovery_jobs": 0, "duplicate_records": 0,
        "invalid_records": 0, "unsupported_period_records": 0}
    receipts, ambiguous = {}, set()

    def register(issuer_id, receipt, form, *, market="KR", corp_code=None, entity_id=None, profile=None):
        issuer = issuers.get(issuer_id)
        if (market != "KR" or issuer is None or issuer[0] != "KR"
                or not re.fullmatch(r"DART:[0-9]{8}", issuer[1])
                or not isinstance(receipt, str) or not re.fullmatch(r"[0-9]{14}", receipt)
                or entity_id is not None and entity_id != receipt
                or corp_code is not None and corp_code != issuer[1][5:]
                or profile not in {None, "periodic_report"} or not isinstance(form, str)):
            stats["invalid_records"] += 1
            return
        periods = re.findall(r"\(([0-9]{4})\.([0-9]{2})\)", form)
        report_kinds = sum(name in form for name in ("사업보고서", "반기보고서", "분기보고서"))
        if len(periods) != 1 or not 1 <= int(periods[0][1]) <= 12 or report_kinds != 1:
            stats["invalid_records"] += 1
            return
        year = periods[0][0]
        if int(year) < 2015:
            stats["unsupported_period_records"] += 1
        code = dart_report_code(form)
        candidates = [code] if code else ["11013", "11014"]
        metadata = {"issuer_id": issuer_id, "receipt": receipt, "corp_code": issuer[1][5:],
            "form": form, "bsns_year": year, "reprt_code_candidates": candidates}
        if receipt in receipts:
            if receipts[receipt] == metadata:
                stats["duplicate_records"] += 1
            else:
                ambiguous.add(receipt)
        else:
            receipts[receipt] = metadata

    stored = select(Filing.issuer_id, Filing.accession, Filing.form).where(Filing.source == "opendart")
    for issuer_id, receipt, form in session.execute(stored).yield_per(2000):
        stats["scanned_filing_records"] += 1
        register(issuer_id, receipt, form)
    request = CollectionJob.request
    discovered = select(request["issuer_id"].astext, request["filing"]["rcept_no"].astext,
        request["filing"]["report_nm"].astext, request["market"].astext,
        request["filing"]["corp_code"].astext, CollectionJob.entity_id,
        request["disclosure_profile"].astext).where(
            CollectionJob.source == "opendart", CollectionJob.domain == "filing_sql")
    for issuer_id, receipt, form, market, corp_code, entity_id, profile in session.execute(discovered).yield_per(2000):
        stats["scanned_discovery_jobs"] += 1
        register(issuer_id, receipt, form, market=market, corp_code=corp_code, entity_id=entity_id, profile=profile)

    pending, reused, eligible, created, planned = [], 0, 0, 0, 0

    def flush_pending():
        nonlocal created
        if pending:
            created += len(session.execute(insert(CollectionJob).values(pending)
                .on_conflict_do_nothing(index_elements=[CollectionJob.identity_hash])
                .returning(CollectionJob.id)).all())
            pending.clear()

    for receipt, metadata in receipts.items():
        if receipt in ambiguous or int(metadata["bsns_year"]) < 2015:
            continue
        eligible += 1
        for scope in ("CFS", "OFS"):
            entity_id = f"{receipt}:{scope}"
            specification = {"market": "KR", **metadata, "fs_div": scope, "disclosure_profile": "periodic_report"}
            identity = stable_hash(["opendart", "filing_json", entity_id, logical_request(specification)])
            if identity in existing:
                reused += 1
                continue
            existing.add(identity)
            planned += 1
            pending.append({"id": new_id(), "identity_hash": identity, "source": "opendart", "domain": "filing_json",
                "entity_id": entity_id, "request": specification, "priority": 1})
            if len(pending) == 500:
                flush_pending()
    flush_pending()
    session.flush()
    return {"created_jobs": created, "eligible_filings": eligible, "reused_jobs": reused + planned - created,
        **stats, "ambiguous_receipts": len(ambiguous), "historical_date_qualification": False}


def collect_dart_json(engine, request, *, dart, fence=None):
    if not re.fullmatch(r"\d{14}", request["receipt"]) or not re.fullmatch(r"\d{8}", request["corp_code"]):
        raise ValueError("Invalid DART identity")
    if request["fs_div"] not in {"CFS", "OFS"} or not re.fullmatch(r"\d{4}", request["bsns_year"]):
        raise ValueError("Invalid DART structured request")
    reason = "structured_accounts_unavailable"
    for code in request["reprt_code_candidates"]:
        if code not in {"11011", "11012", "11013", "11014"}:
            raise ValueError("Invalid DART report code")
        try:
            response, payload = dart.fetch("fnlttSinglAcntAll.json", {"corp_code": request["corp_code"],
                "bsns_year": request["bsns_year"], "reprt_code": code, "fs_div": request["fs_div"]})
        except ProviderError as exc:
            if exc.code not in {"013", "014"}:
                raise
            continue
        try:
            parsed = parse_financial_json(payload, requested_receipt=request["receipt"],
                requested_fs_div=request["fs_div"], requested_corp_code=request["corp_code"])
        except ReceiptMismatch:
            reason = "latest_only_receipt_mismatch"
            continue
        if any(str(row.get("bsns_year", "")) != request["bsns_year"] or str(row.get("reprt_code", "")) != code
               for row in payload.get("list", [])):
            reason = "report_identity_mismatch"
            continue
        if not parsed.inventory.numeric_nodes:
            continue
        with transaction(engine) as session:
            _fence(session, fence)
            document = store_extraction(session, issuer_id=request["issuer_id"], source="opendart",
                accession=request["receipt"], form=request["form"], source_url=dart.base + "fnlttSinglAcntAll.json",
                content=response.content, result=parsed, instance_key=f"structured:{request['fs_div']}",
                available_at=response.captured_at, captured_at=response.captured_at,
                disclosure_profile=request["disclosure_profile"], source_request_metadata={
                    "rcept_no": request["receipt"], "reprt_code": code, "bsns_year": request["bsns_year"],
                    "fs_div": request["fs_div"], "report_code_certification": "response_identity_verified_dates_unqualified"}, fence=fence)
            _fence(session, fence)
            complete = document.extraction_complete
        return {"status": "succeeded" if complete else "unsupported", "reason": None if complete else "source_inventory_or_identity_gap",
            "stored_source_facts": len(parsed.facts), "source_format_complete": complete,
            "verified_financial_scope": request["fs_div"], "availability_precision": "capture_only",
            "fiscal_dates_qualified": False, "document_archived": False}
    return {"status": "unsupported", "reason": reason, "stored_source_facts": 0}
