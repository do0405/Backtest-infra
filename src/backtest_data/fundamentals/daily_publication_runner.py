"""Bounded resumable SQL-only daily metadata backfill over the full manifest.

No provider calls, credentials, original archive retrieval, source mutation, or
sample issuer list. Ordinary document metadata produces one assertion covering
its matching source rows; only exact returned-version mismatches need per-fact
assertions. Progress advances after committed SQL, making replay idempotent.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from ..catalog.models import ExchangeSession, Issuer, Security
from ..db import utcnow
from ..ingestion.models import CollectionJob, TargetUniverseManifest
from .domain import stable_hash
from .models import Filing, FinancialSourceDocument, SourceFinancialFact
from .storage import json_value
from .daily_publication import (POLICY_VERSION, _aware, append_daily_publication_evidence,
    derive_daily_publication, source_version_ref)


MAX_BATCH_DOCUMENTS = 100
FORMAT_VERSION = "financial_daily_publication_progress_v1"
MAX_METADATA_ROWS = 100
METADATA_LOOKUP_TIMEOUT_MS = 1000


def _frozen_descriptor(progress):
    return {k: progress[k] for k in ("format_version", "policy_version", "watermark", "original_target", "total_documents")}


def new_daily_progress(target, *, watermark, total_documents):
    _aware(watermark, "watermark")
    if type(total_documents) is not int or total_documents < 0 or not target.members:
        raise ValueError("full_manifest_and_source_denominator_required")
    progress = dict(format_version=FORMAT_VERSION, policy_version=POLICY_VERSION,
        watermark=watermark.astimezone(timezone.utc).isoformat(),
        original_target=dict(manifest_id=target.id, content_hash=target.content_hash,
            security_count=len(target.members), market_counts=dict(Counter(m["market"] for m in target.members))),
        total_documents=total_documents, cursor=None, status="ready", results={}, coverage_complete=False)
    progress["target_binding_hash"] = stable_hash(_frozen_descriptor(progress))
    return progress


def validate_daily_progress(progress, target):
    if progress.get("format_version") != FORMAT_VERSION or progress.get("policy_version") != POLICY_VERSION:
        raise ValueError("daily_progress_policy_mismatch")
    expected = progress.get("original_target", {})
    if (expected.get("manifest_id") != target.id or expected.get("content_hash") != target.content_hash
            or expected.get("security_count") != len(target.members)
            or expected.get("market_counts") != dict(Counter(m["market"] for m in target.members))):
        raise ValueError("daily_progress_full_target_mismatch")
    _aware(datetime.fromisoformat(progress["watermark"]), "watermark")
    if progress.get("target_binding_hash") != stable_hash(_frozen_descriptor(progress)):
        raise ValueError("daily_progress_watermark_or_denominator_mismatch")
    if progress.get("cursor") is not None and (not isinstance(progress["cursor"], str) or not progress["cursor"]):
        raise ValueError("daily_progress_cursor_invalid")
    return progress


def daily_target_query(*, watermark, after_document_id=None, limit=25, issuer_ids=None):
    _aware(watermark, "watermark")
    if type(limit) is not int or not 1 <= limit <= MAX_BATCH_DOCUMENTS:
        raise ValueError("bounded_document_batch_required")
    query = select(FinancialSourceDocument, Filing, Issuer).join(Filing,
        FinancialSourceDocument.filing_id == Filing.id).join(Issuer, Filing.issuer_id == Issuer.id).where(
        FinancialSourceDocument.captured_at <= watermark, Filing.captured_at <= watermark,
        Filing.source.in_(("sec", "opendart")))
    if issuer_ids is not None:
        query = query.where(Issuer.id.in_(tuple(sorted(issuer_ids))))
    if after_document_id is not None:
        if not isinstance(after_document_id, str) or not after_document_id:
            raise ValueError("document_cursor_invalid")
        query = query.where(FinancialSourceDocument.id > after_document_id)
    return query.order_by(FinancialSourceDocument.id).limit(limit)


def _full_issuer_ids(session, target):
    security_ids = {member.get("security_id") for member in target.members}
    if None in security_ids or not security_ids:
        raise ValueError("full_target_security_identity_required")
    rows = session.execute(select(Security.id, Security.issuer_id).where(Security.id.in_(security_ids))).all()
    if len(rows) != len(security_ids):
        raise ValueError("full_target_security_missing")
    ids = {issuer for _, issuer in rows if issuer}
    ids.update(member["issuer_id"] for member in target.members if member.get("issuer_id"))
    return ids


def daily_metadata_lookup_query(filing, version):
    request = CollectionJob.request
    match = request["filing"]["rcept_no"].astext == version if filing.source == "opendart" else request["filing"]["accessionNumber"].astext == version
    # entity_id uses ix_collection_job_coverage_entity; JSON filters apply only
    # after that narrow index lookup, never as a whole-queue mandatory scan.
    return select(request).where(CollectionJob.source == filing.source, CollectionJob.domain == "filing_sql",
        CollectionJob.entity_id == version, request["issuer_id"].astext == filing.issuer_id,
        match).order_by(CollectionJob.id).limit(MAX_METADATA_ROWS + 1)


def _metadata_records(session, filing, version):
    key = "rcept_no" if filing.source == "opendart" else "accessionNumber"
    try:
        with session.begin_nested():
            prior = session.scalar(text("SHOW statement_timeout"))
            session.execute(text("SELECT set_config('statement_timeout', :timeout, true)"),
                            {"timeout": f"{METADATA_LOOKUP_TIMEOUT_MS}ms"})
            records = session.scalars(daily_metadata_lookup_query(filing, version)).all()
            session.execute(text("SELECT set_config('statement_timeout', :timeout, true)"), {"timeout": prior})
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "57014":
            raise
        return [{key: version, "_lookup_issue": "publication_metadata_lookup_timeout"}]
    if len(records) > MAX_METADATA_ROWS:
        return [{key: version, "_lookup_issue": "publication_metadata_lookup_row_limit"}]
    # Only regulator metadata fields are retained; request transport/auth fields
    # never cross into evidence or progress files.
    allowed = {"rcept_no", "rcept_dt", "report_nm", "corp_code", "accessionNumber", "filingDate", "form",
               "accn", "filed", "published_date", "public_day", "correction_publication_date", "revision_public_day"}
    return [{k: v for k, v in (record.get("filing", {}) or {}).items() if k in allowed} for record in records]


def _with_calendar(session, document, filing, issuer, *, source_fact=None, metadata_records=()):
    result = derive_daily_publication(document, filing, market=issuer.market, source_fact=source_fact,
                                      metadata_records=metadata_records)
    if result["public_day"] is not None:
        sessions = session.scalars(select(ExchangeSession).where(ExchangeSession.market == issuer.market,
            ExchangeSession.session > result["public_day"],
            ExchangeSession.session <= result["public_day"] + timedelta(days=31)).order_by(ExchangeSession.session)).all()
        if sessions:
            result = derive_daily_publication(document, filing, market=issuer.market, source_fact=source_fact,
                metadata_records=metadata_records, sessions=sessions)
    return result


def _returned_version_expression(filing):
    evidence = SourceFinancialFact.evidence
    fields = [evidence["source_row"]["rcept_no"].astext, evidence["returned_rcept_no"].astext,
              evidence["receipt"].astext] if filing.source == "opendart" else [
              evidence["source_row"]["accn"].astext, evidence["source_row"]["accessionNumber"].astext,
              evidence["accn"].astext]
    return func.coalesce(*(func.nullif(field, "") for field in fields), filing.accession)


def daily_source_metadata_query(*, document_id, filing, watermark):
    """Distinct publication metadata on matching source rows, never periods."""
    _aware(watermark, "watermark")
    field = "rcept_dt" if filing.source == "opendart" else "filed"
    evidence = SourceFinancialFact.evidence
    day = func.coalesce(func.nullif(evidence["source_row"][field].astext, ""),
                        func.nullif(evidence[field].astext, ""))
    return select(day).where(SourceFinancialFact.document_id == document_id,
        _returned_version_expression(filing) == filing.accession, day.is_not(None),
        SourceFinancialFact.captured_at <= watermark, SourceFinancialFact.extracted_at <= watermark,
        SourceFinancialFact.first_seen_at <= watermark).distinct()


def backfill_daily_document(session, *, document, filing, issuer, watermark, recorded_at=None):
    """One document assertion plus streamed authoritative mismatch exceptions."""
    _aware(watermark, "watermark")
    now = _aware(recorded_at or utcnow(), "recorded_at")
    metadata = []
    resolution = derive_daily_publication(document, filing, market=issuer.market)
    if resolution["status"] != "qualified" or resolution["metadata_date_basis"] == "receipt_date_encoded_fallback":
        row_dates = session.scalars(daily_source_metadata_query(document_id=document.id, filing=filing, watermark=watermark)).all()
        identity_key, day_key = ("rcept_no", "rcept_dt") if filing.source == "opendart" else ("accn", "filed")
        metadata = [{identity_key: filing.accession, day_key: day} for day in row_dates]
        resolution = derive_daily_publication(document, filing, market=issuer.market, metadata_records=metadata)
    # DART's declared receipt-date fallback is usable without queue discovery.
    # Only otherwise unresolved metadata needs a bounded indexed queue lookup.
    if resolution["status"] != "qualified":
        metadata += _metadata_records(session, filing, filing.accession)
    resolution = _with_calendar(session, document, filing, issuer, metadata_records=metadata)
    lookup_issues = [x["_lookup_issue"] for x in metadata if "_lookup_issue" in x]
    if resolution["status"] != "qualified" and lookup_issues:
        resolution["reason"] = lookup_issues[0]
    append_daily_publication_evidence(session, document=document, filing=filing, resolution=resolution,
        captured_at=now, recorded_at=now)
    statuses = Counter({resolution["status"]: 1})
    reasons = Counter({resolution["reason"]: 1}) if resolution["reason"] else Counter()
    mismatches = 0
    query = select(SourceFinancialFact).where(SourceFinancialFact.document_id == document.id,
        _returned_version_expression(filing) != filing.accession,
        SourceFinancialFact.captured_at <= watermark, SourceFinancialFact.extracted_at <= watermark,
        SourceFinancialFact.first_seen_at <= watermark).order_by(SourceFinancialFact.id).execution_options(yield_per=250)
    metadata_cache = {}
    for fact in session.scalars(query):
        mismatches += 1
        version = source_version_ref(filing, fact)
        initial = derive_daily_publication(document, filing, market=issuer.market, source_fact=fact)
        if version not in metadata_cache:
            metadata_cache[version] = [] if initial["status"] == "qualified" else _metadata_records(session, filing, version)
        # Dates may be explicitly present only on a particular source row, so
        # share metadata lookups but never reuse another fact's resolution.
        specific = _with_calendar(session, document, filing, issuer, source_fact=fact,
                                  metadata_records=metadata_cache[version])
        append_daily_publication_evidence(session, document=document, filing=filing, source_fact=fact,
            resolution=specific, captured_at=now, recorded_at=now)
        statuses[specific["status"]] += 1
        if specific["reason"]:
            reasons[specific["reason"]] += 1
    return dict(document_id=document.id, source=filing.source, status=resolution["status"],
        reason=resolution["reason"], assertions=dict(statuses), fact_specific_assertions=mismatches,
        reasons=dict(reasons), source_version_ref=resolution["source_version_ref"], evidence_grade="metadata_grade")


def _write_progress(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".pending")
    pending.write_text(json.dumps(json_value(value), ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)


def backfill_daily_publications(database, *, manifest_id, progress_path, budget_seconds=300,
                                batch_size=25, watermark=None, max_documents=None):
    """Commit bounded batches, then cursor; budget deferral preserves denominator."""
    if (type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH_DOCUMENTS
            or isinstance(budget_seconds, bool) or not isinstance(budget_seconds, (int, float)) or budget_seconds <= 0
            or (max_documents is not None and (type(max_documents) is not int or max_documents < 1))):
        raise ValueError("valid_bounded_daily_backfill_budget_required")
    path = Path(progress_path)
    started = time.monotonic()
    visited = 0
    with database.connect().execution_options(isolation_level="READ COMMITTED") as connection, Session(connection) as session:
        if not session.scalar(text("SELECT pg_try_advisory_lock(hashtext('financial_daily_metadata_backfill_v1'))")):
            raise ValueError("daily_publication_backfill_already_running")
        session.commit()
        try:
            with session.begin():
                target = session.get(TargetUniverseManifest, manifest_id)
                if target is None:
                    raise ValueError("original_full_target_manifest_missing")
                issuer_ids = _full_issuer_ids(session, target)
                if path.exists():
                    progress = validate_daily_progress(json.loads(path.read_text(encoding="utf-8")), target)
                    if watermark is not None and _aware(watermark, "watermark").astimezone(timezone.utc).isoformat() != progress["watermark"]:
                        raise ValueError("daily_progress_watermark_mismatch")
                    frozen_watermark = datetime.fromisoformat(progress["watermark"])
                else:
                    frozen_watermark = _aware(watermark or utcnow(), "watermark")
                    statement = daily_target_query(watermark=frozen_watermark, issuer_ids=issuer_ids).limit(None).order_by(None).subquery()
                    total = session.scalar(select(func.count()).select_from(statement))
                    progress = new_daily_progress(target, watermark=frozen_watermark, total_documents=total)
            _write_progress(path, progress)
            while time.monotonic() - started < budget_seconds and (max_documents is None or visited < max_documents):
                limit = batch_size if max_documents is None else min(batch_size, max_documents - visited)
                with session.begin():
                    rows = session.execute(daily_target_query(watermark=frozen_watermark, issuer_ids=issuer_ids,
                        after_document_id=progress["cursor"], limit=limit)).all()
                    outcomes = [backfill_daily_document(session, document=document, filing=filing, issuer=issuer,
                        watermark=frozen_watermark) for document, filing, issuer in rows]
                    next_cursor = rows[-1][0].id if rows else None
                if not rows:
                    progress["status"] = "source_pass_complete"
                    break
                for outcome in outcomes:
                    progress["results"][outcome["document_id"]] = outcome
                progress["cursor"] = next_cursor
                visited += len(rows)
                progress["status"] = "running"
                progress["checked_at"] = utcnow().isoformat()
                _write_progress(path, progress)
            else:
                progress["status"] = "budget_deferred"
            progress["checked_at"] = utcnow().isoformat()
            _write_progress(path, progress)
        finally:
            if session.in_transaction():
                session.rollback()
            session.execute(text("SELECT pg_advisory_unlock(hashtext('financial_daily_metadata_backfill_v1'))"))
            session.commit()
    counts = Counter(x["status"] for x in progress["results"].values())
    return dict(status=progress["status"], policy_version=POLICY_VERSION, evidence_grade="metadata_grade",
        original_target=progress["original_target"], watermark=progress["watermark"], total_documents=progress["total_documents"],
        documents_with_outcomes=len(progress["results"]), qualified_documents=counts["qualified"],
        unavailable_documents=counts["unavailable"], pending_documents=max(0, progress["total_documents"] - len(progress["results"])),
        cursor=progress["cursor"], progress_path=str(path), coverage_complete=False)
