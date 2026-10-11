"""Resumable regulator filing discovery and structured SQL extraction."""
from datetime import date
import json
import re

from sqlalchemy import select

from ..catalog.models import Issuer, Security
from ..db import transaction, utcnow
from ..ingestion.jobs import plan_job
from ..ingestion.http import ProviderError
from .storage import _fence, store_extraction
from .xbrl import parse_xbrl, parse_xbrl_zip


PERIODIC_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}


def dart_report_code(title):
    if "사업보고서" in title:
        return "11011"
    if "반기보고서" in title:
        return "11012"
    # A March period may be Q3 for a June-year-end issuer. A historical
    # company calendar is required before assigning a quarter form code.
    return None


def _sec_rows(data):
    if "filings" in data:
        data = data["filings"]["recent"]
    accessions = data.get("accessionNumber", [])
    return [{key: values[i] for key, values in data.items() if isinstance(values, list) and len(values) == len(accessions)}
            for i in range(len(accessions))]


def discover_issuer(engine, *, issuer_id, market, request, dart, sec, fence=None):
    with transaction(engine) as session:
        _fence(session, fence)
        issuer = session.get(Issuer, issuer_id)
        _fence(session, fence)
        identifier = issuer.source_identifier
    created = 0
    if market == "KR":
        corp_code = identifier.removeprefix("DART:")
        page = 1
        while True:
            try:
                response, payload = dart.fetch("list.json", {"corp_code": corp_code, "bgn_de": request.get('discovery_start','1999-01-01').replace('-',''), "end_de": request["output_end"].replace("-", ""),
                    "last_reprt_at": "N", "pblntf_ty": "A", "page_count": 100, "page_no": page})
            except ProviderError as exc:
                if exc.code == "013":
                    break
                raise
            with transaction(engine) as session:
                _fence(session, fence)
                for filing in payload.get("list", []):
                    title = filing.get("report_nm", "")
                    if not any(name in title for name in ("사업보고서", "반기보고서", "분기보고서")):
                        continue
                    code = dart_report_code(title)
                    if not re.fullmatch(r"\d{14}", filing.get("rcept_no", "")):
                        raise ValueError("DART accession identity invalid")
                    child = {**request, "issuer_id": issuer_id, "filing": filing, "reprt_code": code,
                             "reprt_code_candidates": ["11013", "11014"] if "분기보고서" in title and code is None else [],
                             "disclosure_profile": "periodic_report"}
                    _fence(session, fence)
                    plan_job(session, source="opendart", domain="filing_sql", entity_id=filing["rcept_no"], request=child, priority=1)
                    _fence(session, fence)
                    created += 1
                _fence(session, fence)
            if page >= int(payload.get("total_page", 1)):
                break
            page += 1
    else:
        response = sec.fetch(f"https://data.sec.gov/submissions/{identifier}.json")
        payload = json.loads(response.content)
        rows = _sec_rows(payload)
        for shard in payload.get("filings", {}).get("files", []):
            if request.get('discovery_start') and shard.get('filingTo') and shard['filingTo']<request['discovery_start']:
                continue
            with transaction(engine) as session:
                _fence(session, fence)
            name = shard.get("name", "")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+\.json", name) or ".." in name:
                raise ValueError("SEC shard filename invalid")
            rows.extend(_sec_rows(json.loads(sec.fetch("https://data.sec.gov/submissions/" + name).content)))
        with transaction(engine) as session:
            _fence(session, fence)
            for filing in rows:
                if (filing.get("form") not in PERIODIC_FORMS or filing.get("filingDate", "") > request["output_end"]
                    or request.get('discovery_start') and filing.get('filingDate','')<request['discovery_start']):
                    continue
                accession = filing.get("accessionNumber", "")
                if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
                    raise ValueError("SEC accession identity invalid")
                child = {**request, "issuer_id": issuer_id, "cik": identifier.removeprefix("CIK"), "filing": filing,
                         "disclosure_profile": "periodic_report"}
                _fence(session, fence)
                plan_job(session, source="sec", domain="filing_sql", entity_id=accession, request=child, priority=1)
                _fence(session, fence)
                created += 1
            _fence(session, fence)
    return {"child_filing_jobs": created, "warmup_strategy": "all_available_periodic_filings_before_output_end",
            "disclosure_profile": "periodic_report"}


def collect_filing(engine, job_request, *, source, dart, sec, fence=None):
    filing = job_request["filing"]
    request_evidence = {}
    if source == "opendart":
        codes = [job_request["reprt_code"]] if job_request.get("reprt_code") else job_request.get("reprt_code_candidates", [])
        if not codes and "분기보고서" in filing.get("report_nm", ""):
            codes = ["11013", "11014"]
        if not codes:
            return {"status": "unsupported", "reason": "fiscal_report_code_requires_verified_company_calendar"}
        accession = filing["rcept_no"]
        response = None
        for code in codes:
            if code not in {"11011", "11012", "11013", "11014"}:
                raise ValueError("Unregistered report-code candidate")
            try:
                response = dart.fetch("fnlttXbrl.xml", {"rcept_no": accession, "reprt_code": code}, json_response=False)
                break
            except ProviderError as exc:
                if exc.code not in {"013", "014"}:
                    raise
        if response is None:
            return {"status": "unsupported", "reason": "original_receipt_xbrl_not_available"}
        request_evidence = {"rcept_no": accession, "reprt_code": code,
                            "report_code_certification": "transport_only_fiscal_ordinal_unverified"}
        results = parse_xbrl_zip(response.content)
        source_url = dart.base + "fnlttXbrl.xml"
        form = filing.get("report_nm", "periodic")
    else:
        accession = filing["accessionNumber"]
        base = f"https://www.sec.gov/Archives/edgar/data/{int(job_request['cik'])}/{accession.replace('-', '')}/"
        index = json.loads(sec.fetch(base + "index.json").content)
        names = [item["name"] for item in index.get("directory", {}).get("item", [])]
        candidates = [name for name in names if name.endswith("_htm.xml")]
        if not candidates:
            candidates = [name for name in names if name.endswith(".xml") and not name.endswith(("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml"))
                          and name not in {"FilingSummary.xml"}]
        if not candidates:
            candidates = [filing.get("primaryDocument", "")]
        results, response, source_url = [], None, None
        for name in candidates:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or ".." in name:
                continue
            candidate_response = sec.fetch(base + name)
            try:
                from .completion import parse_sec_xbrl
                parsed = parse_sec_xbrl(candidate_response.content,form=filing['form'])
            except ValueError:
                continue
            if parsed.inventory.numeric_nodes:
                results, response, source_url = [parsed], candidate_response, base + name
                break
        if response is None:
            return {"status": "unsupported", "reason": "supported_numeric_instance_unavailable"}
        form = filing["form"]
    numeric_results = [result for result in results if result.inventory.numeric_nodes]
    if not numeric_results:
        return {"status": "unsupported", "reason": "numeric_instance_unavailable"}
    stored, complete = 0, True
    with transaction(engine) as session:
        _fence(session, fence)
        for offset, result in enumerate(numeric_results):
            # Each member has its own parser identity even when a ZIP contains multiple instances.
            document = store_extraction(session, issuer_id=job_request["issuer_id"], source=source, accession=accession,
                form=form, source_url=source_url, content=response.content, result=result, instance_key=str(offset),
                available_at=response.captured_at, captured_at=response.captured_at,
                disclosure_profile=job_request["disclosure_profile"], source_request_metadata=request_evidence, fence=fence)
            stored += len(result.facts)
            complete &= document.extraction_complete
        _fence(session, fence)
    return {"status": "succeeded" if complete else "unsupported", "reason": None if complete else "source_inventory_or_identity_gap",
            "stored_source_facts": stored, "source_format_complete": complete,
            "availability_precision": "capture_only", "historical_public_availability_qualified": False,
            "document_archived": False, "canonical_mapping_complete": False}
