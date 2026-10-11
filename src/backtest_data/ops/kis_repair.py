"""Bounded, immutable KIS archive recovery; never performs provider HTTP calls."""

from collections import Counter, defaultdict
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time

from sqlalchemy import func, select

from ..catalog.models import Security, SourceContract
from ..db import new_id, transaction, utcnow
from ..ingestion.http import ProviderResponse
from ..ingestion.jobs import stable_hash
from ..ingestion.models import CollectionJob, IngestionAttempt, RawObject
from ..ingestion.raw_store import RawStore
from ..prices.adapters.kis import KISDailyRequest
from ..prices.domain import require_aware
from ..prices.storage import KIS_NORMALIZER_VERSION, KIS_SPEC, store_kis_prices
from ..releases.models import ValidationIssue


PARAMETERS = frozenset(KISDailyRequest("005930", date(2020, 1, 1), date(2020, 1, 2)).params())
COUNTERS = ("processed_original_captures", "inserted", "source_rows_revalidated", "supplier_zero_transaction_rows")


def _json(content):
    def nonfinite(value):
        raise ValueError("nonfinite JSON constant")
    try:
        return json.loads(content, parse_constant=nonfinite)
    except (ValueError, UnicodeError):
        raise ValueError("KIS recovery requires valid finite JSON evidence") from None


def _request(params):
    if not isinstance(params, dict) or set(params) != PARAMETERS or any(not isinstance(value, str) for value in params.values()):
        raise ValueError("KIS recovery requires exactly six string request parameters")
    if (params["FID_COND_MRKT_DIV_CODE"] != "J" or params["FID_PERIOD_DIV_CODE"] != "D"
            or params["FID_ORG_ADJ_PRC"] != "1"):
        raise ValueError("KIS recovery requires explicit J/daily/raw-option-1 request")
    try:
        dates = [params[name] for name in ("FID_INPUT_DATE_1", "FID_INPUT_DATE_2")]
        if any(not re.fullmatch(r"[0-9]{8}", value) for value in dates):
            raise ValueError
        request = KISDailyRequest(params["FID_INPUT_ISCD"], *(datetime.strptime(value, "%Y%m%d").date() for value in dates))
    except (ValueError, TypeError):
        raise ValueError("KIS recovery request dates or security code are invalid") from None
    if request.params() != params:
        raise ValueError("KIS recovery request does not round-trip exactly")
    return request


def _audit(audit_path):
    content = Path(audit_path).read_bytes()
    audit = _json(content)
    recovery = audit.get("recovery") if isinstance(audit, dict) else None
    groups = recovery.get("groups") if isinstance(recovery, dict) else None
    if not isinstance(groups, list) or not groups:
        raise ValueError("KIS recovery audit requires nonempty recovery.groups")
    identities = set()
    for group in groups:
        if not isinstance(group, dict) or any(not isinstance(group.get(key), str) or not group[key].strip()
                for key in ("security_id", "raw_id")):
            raise ValueError("KIS recovery audit requires explicit security and raw IDs")
        _request(group.get("request"))
        if (type(group.get("eligible_issues")) is not int or group["eligible_issues"] <= 0
                or type(group.get("remaining_issues")) is not int or group["remaining_issues"] != 0):
            raise ValueError("KIS recovery audit contains unproven or remaining issues")
        identity = stable_hash([group["security_id"], group["raw_id"], group["request"]])
        if identity in identities:
            raise ValueError("KIS recovery audit contains duplicate request groups")
        identities.add(identity)
    return groups, hashlib.sha256(content).hexdigest()


def _progress_path(path):
    if path is None:
        return None
    candidate = Path(path).resolve()
    root = (Path.cwd() / "artifacts").resolve()
    if not candidate.is_relative_to(root) or candidate == root:
        raise ValueError("KIS recovery progress must be a file inside workspace artifacts")
    return candidate


def _write_progress(path, state):
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix="kis-replay-progress-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def _state(path, audit_hash, raw_root, group_count):
    state = {"format_version": 1, "audit_hash": audit_hash, "normalizer_version": KIS_NORMALIZER_VERSION,
        "raw_root": str(Path(raw_root).resolve()), "status": "partial", "next_group_index": 0,
        "next_attempt_index": 0, "original_attempt_ids": [], "processed_groups": 0,
        "history_coverage_qualified": False, **dict.fromkeys(COUNTERS, 0)}
    if path is not None and path.exists():
        saved = _json(path.read_bytes())
        if not isinstance(saved, dict) or any(saved.get(key) != state[key]
                for key in ("format_version", "audit_hash", "normalizer_version", "raw_root")):
            raise ValueError("KIS recovery progress does not match audit, archive root, or normalizer")
        if (any(type(saved.get(key)) is not int or saved[key] < 0
                for key in (*COUNTERS, "next_group_index", "next_attempt_index", "processed_groups"))
                or saved["next_group_index"] > group_count or saved["processed_groups"] != saved["next_group_index"]
                or not isinstance(saved.get("original_attempt_ids"), list)):
            raise ValueError("KIS recovery progress cursor or counters are invalid")
        state.update(saved)
    return state


def _context(session, groups, audit_hash, *, validate_audit=True):
    """Batch unindexed evidence lookups once; retain compact audit-scoped rows."""
    security_ids, raw_ids = {item["security_id"] for item in groups}, {item["raw_id"] for item in groups}
    securities = dict(session.execute(select(Security.id, Security.market).where(Security.id.in_(security_ids))).all())
    raws = {row["id"]: dict(row) for row in session.execute(select(RawObject.id, RawObject.content_hash,
        RawObject.storage_key, RawObject.byte_count).where(RawObject.id.in_(raw_ids))).mappings()}
    hashes = {item["content_hash"] for item in raws.values()}
    security_key, raw_key = ValidationIssue.evidence["security_id"].astext, ValidationIssue.evidence["raw_id"].astext
    request_key = ValidationIssue.evidence["request"]
    issues = Counter()
    for rule, security_id, raw_id, params, count in session.execute(select(ValidationIssue.rule, security_key,
            raw_key, request_key, func.count()).where(ValidationIssue.record_domain == "price_observation",
            security_key.in_(security_ids)).group_by(ValidationIssue.rule, security_key, raw_key, request_key)):
        issues[(rule, security_id, raw_id, stable_hash(params))] += count
    originals, receipts = defaultdict(list), defaultdict(list)
    columns = (IngestionAttempt.id, IngestionAttempt.content_hash, IngestionAttempt.request_hash,
        IngestionAttempt.captured_at, IngestionAttempt.evidence)
    scope = (SourceContract.source == "kis", SourceContract.domain == "prices", SourceContract.market == "KR")
    for row in session.execute(select(*columns).join(SourceContract).where(*scope,
            IngestionAttempt.status == "received", IngestionAttempt.content_hash.in_(hashes))
            .order_by(IngestionAttempt.captured_at, IngestionAttempt.id)).mappings():
        params = row["evidence"].get("params")
        if isinstance(params, dict):
            originals[(row["content_hash"], stable_hash(params))].append(dict(row))
    for row in session.execute(select(*columns).join(SourceContract).where(*scope,
            IngestionAttempt.status == "replay_receipt",
            IngestionAttempt.evidence["normalizer_version"].astext == KIS_NORMALIZER_VERSION,
            IngestionAttempt.evidence["replay_evidence"]["audit_hash"].astext == audit_hash)
            .order_by(IngestionAttempt.id)).mappings():
        proof = row["evidence"].get("replay_evidence", {})
        receipts[(proof.get("audit_group_index"), proof.get("original_ingestion_attempt_id"))].append(dict(row))
    context = {"securities": securities, "raws": raws, "issues": issues, "originals": originals, "receipts": receipts,
        "other_issue_identities": {key[1:] for key in issues if key[0] != "source_row_validation"}}
    # Bind every audited security/request/count before any group can write rows.
    for group in groups:
        if validate_audit:
            _issue_binding(group, context)
        raw = raws.get(group["raw_id"])
        if raw is None:
            raise ValueError("KIS recovery audit references a missing raw object")
        _originals(raw["content_hash"], group, context)
    return context


def _issue_binding(group, context):
    identity = (group["security_id"], group["raw_id"], stable_hash(group["request"]))
    issues = context["issues"]
    if (issues[("source_row_validation", *identity)] != group["eligible_issues"]
            or identity in context["other_issue_identities"]):
        raise ValueError("KIS recovery audit security/request/count does not match SQL validation issue evidence")


def _group(session, store, group, context):
    _issue_binding(group, context)
    if context["securities"].get(group["security_id"]) != "KR":
        raise ValueError("KIS recovery security must exist in market KR")
    raw = context["raws"].get(group["raw_id"])
    if raw is None or not re.fullmatch(r"[0-9a-f]{64}", raw["content_hash"]):
        raise ValueError("KIS recovery raw object requires valid SHA256")
    canonical_key = f"sha256/{raw['content_hash'][:2]}/{raw['content_hash']}.gz"
    if raw["storage_key"] != canonical_key:
        raise ValueError("KIS recovery archive storage key must match its canonical SHA256 path")
    content = store.read(raw["storage_key"])
    if hashlib.sha256(content).hexdigest() != raw["content_hash"] or len(content) != raw["byte_count"]:
        raise ValueError("KIS recovery archive hash or byte count mismatch")
    request = _request(group["request"])
    payload = _json(content)
    if not isinstance(payload, dict) or str(payload.get("rt_cd")) != "0" or not isinstance(payload.get("output2"), list):
        raise ValueError("KIS recovery archive is not a successful domestic daily response")
    captures = _originals(raw["content_hash"], group, context)
    return request, content, payload, captures


def _originals(content_hash, group, context):
    originals = context["originals"].get((content_hash, stable_hash(group["request"])), [])
    if not originals:
        raise ValueError("KIS recovery requires matching original received capture events")
    captures = []
    for original in originals:
        if (original["request_hash"] != stable_hash(group["request"])
                or original["evidence"].get("endpoint") != KIS_SPEC["endpoint"]
                or original["evidence"].get("params") != group["request"]
                or not isinstance(original["captured_at"], datetime)):
            raise ValueError("KIS recovery original request metadata is contradictory")
        require_aware(original["captured_at"], "original captured_at")
        captures.append({"id": original["id"], "captured_at": original["captured_at"],
            "content_hash": content_hash, "params": deepcopy(original["evidence"]["params"])})
    return captures


def _receipt(audit_hash, group_index, original, context):
    matches = context["receipts"].get((group_index, original["id"]), [])
    for attempt in matches:
        result = attempt["evidence"].get("archive_replay_result")
        if (attempt["content_hash"] == original["content_hash"] and attempt["captured_at"] == original["captured_at"]
                and attempt["evidence"].get("params") == original["params"]
                and attempt["request_hash"] == stable_hash(original["params"])
                and attempt["evidence"].get("replay_evidence", {}).get("audit_hash") == audit_hash
                and isinstance(result, dict) and result.get("response_valid") is True and result.get("pagination_valid") is True
                and result.get("rejected") == 0 and isinstance(result.get("inserted_record_ids"), list)
                and type(result.get("inserted")) is int and len(result["inserted_record_ids"]) == result["inserted"]):
            return result
    return None


def _validate_prefix(session, state, groups, context):
    """A cursor may skip only ALL original captures with complete SQL receipts."""
    for index, group in enumerate(groups[:state["next_group_index"]]):
        raw = context["raws"].get(group["raw_id"])
        if raw is None or context["securities"].get(group["security_id"]) != "KR":
            raise ValueError("KIS recovery progress references missing KR archive evidence")
        captures = _originals(raw["content_hash"], group, context)
        if any(_receipt(state["audit_hash"], index, original, context) is None for original in captures):
            raise ValueError("KIS recovery progress skips original captures without durable complete receipts")


def replay_kis_price_audit(engine, raw_root, audit_path, progress_path=None, budget_seconds=None):
    """Replay every original received event, preserving original capture times.

    Each event and its SQL receipt commit together. File checkpoint lag therefore
    reuses the SQL receipt instead of inventing a capture or duplicating rows.
    A budget bounds execution only; the complete audit remains the denominator.
    """
    if budget_seconds is not None and (isinstance(budget_seconds, bool)
            or not isinstance(budget_seconds, (int, float)) or not math.isfinite(budget_seconds) or budget_seconds <= 0):
        raise ValueError("KIS recovery requires a finite positive execution budget")
    path = _progress_path(progress_path)
    groups, audit_hash = _audit(audit_path)
    state = _state(path, audit_hash, raw_root, len(groups))
    started = time.monotonic()
    with transaction(engine) as session:
        context = _context(session, groups, audit_hash)
        _validate_prefix(session, state, groups, context)
    store, inserted_this_run = RawStore(Path(raw_root)), 0

    def report():
        complete = state["next_group_index"] == len(groups)
        state["status"] = "complete" if complete else "partial"
        return {**state, "inserted_this_run": inserted_this_run, "total_groups": len(groups),
            "recovered_security_ids": sorted({group["security_id"] for group in groups}) if complete else [],
            "history_coverage_qualified": False}

    while state["next_group_index"] < len(groups):
        if budget_seconds is not None and time.monotonic() - started >= budget_seconds:
            _write_progress(path, state)
            return report()
        index = state["next_group_index"]
        group = groups[index]
        with transaction(engine) as session:
            request, content, payload, captures = _group(session, store, group, context)
        ids = [item["id"] for item in captures]
        if state["original_attempt_ids"] and state["original_attempt_ids"] != ids:
            raise ValueError("KIS recovery original capture set changed during resumable replay")
        state["original_attempt_ids"] = ids
        if state["next_attempt_index"] > len(captures):
            raise ValueError("KIS recovery original capture cursor is invalid")
        with transaction(engine) as session:
            if any(_receipt(audit_hash, index, original, context) is None for original in captures[:state["next_attempt_index"]]):
                raise ValueError("KIS recovery progress skips original captures without complete receipts")
        _write_progress(path, state)
        for offset in range(state["next_attempt_index"], len(captures)):
            if budget_seconds is not None and time.monotonic() - started >= budget_seconds:
                _write_progress(path, state)
                return report()
            original = captures[offset]
            appended_receipt = None
            with transaction(engine) as session:
                current = session.get(IngestionAttempt, original["id"], with_for_update=True)
                if (current is None or current.status != "received" or current.content_hash != original["content_hash"]
                        or current.captured_at != original["captured_at"] or current.evidence.get("params") != group["request"]):
                    raise ValueError("KIS recovery original capture changed before atomic replay")
                outcome = _receipt(audit_hash, index, original, context)
                if outcome is None:
                    proof = {"original_ingestion_attempt_id": original["id"], "audit_hash": audit_hash,
                        "audit_group_index": index, "raw_id": group["raw_id"], "request_hash": stable_hash(group["request"]),
                        "repair_kind": "supplier_zero_transaction_archive_replay"}
                    outcome = store_kis_prices(session, group["security_id"], request,
                        ProviderResponse(content, 200, original["captured_at"], "application/json"), payload,
                        Path(raw_root), replay_evidence=proof)
                    if not outcome["response_valid"] or not outcome["pagination_valid"] or outcome["rejected"]:
                        raise ValueError("KIS recovery replay contains unresolved validation or pagination issues")
                    attempt = session.get(IngestionAttempt, outcome["ingestion_attempt_id"])
                    # IngestionAttempt has an immutable SQL trigger even before
                    # commit. Append a receipt; never update the replay attempt.
                    receipt_evidence = {**attempt.evidence, "replay_ingestion_attempt_id": attempt.id,
                        "receipt_recorded_at": utcnow().isoformat(), "archive_replay_result": {
                            key: outcome[key] for key in ("response_valid", "pagination_valid", "rejected", "inserted",
                                "inserted_record_ids", "source_row_count", "supplier_zero_transaction_rows")}}
                    session.add(IngestionAttempt(id=new_id(), source_contract_id=attempt.source_contract_id,
                        request_hash=attempt.request_hash, content_hash=attempt.content_hash,
                        status="replay_receipt", captured_at=original["captured_at"],
                        evidence=receipt_evidence))
                    appended_receipt = {"content_hash": attempt.content_hash, "request_hash": attempt.request_hash,
                        "captured_at": original["captured_at"], "evidence": receipt_evidence}
                    inserted_this_run += outcome["inserted"]
                state["processed_original_captures"] += 1
                state["inserted"] += outcome["inserted"]
                state["source_rows_revalidated"] += outcome["source_row_count"]
                state["supplier_zero_transaction_rows"] += outcome["supplier_zero_transaction_rows"]
            if appended_receipt is not None:
                context["receipts"][(index, original["id"])].append(appended_receipt)
            state["next_attempt_index"] = offset + 1
            _write_progress(path, state)
        state["next_group_index"] += 1
        state["processed_groups"] += 1
        state["next_attempt_index"], state["original_attempt_ids"] = 0, []
        _write_progress(path, state)
    result = report()
    _write_progress(path, result)
    return result


def requeue_recovered_kis_jobs(engine, audit_path, recovery_receipt):
    """Explicitly resume only proven recovered KR validation-gap jobs.

    This never erases historical issues or starts from the output end again.
    Unknown source history remains unqualified after archive recovery.
    """
    groups, audit_hash = _audit(audit_path)
    if (not isinstance(recovery_receipt, Mapping) or recovery_receipt.get("status") != "complete"
            or recovery_receipt.get("audit_hash") != audit_hash or recovery_receipt.get("normalizer_version") != KIS_NORMALIZER_VERSION
            or recovery_receipt.get("processed_groups") != len(groups)):
        raise ValueError("KIS requeue requires a complete matching archive recovery receipt")
    expected, per_security = Counter(), defaultdict(list)
    for index, group in enumerate(groups):
        expected[(group["security_id"], group["raw_id"], stable_hash(group["request"]))] = group["eligible_issues"]
        per_security[group["security_id"]].append((index, group))
    qualified, observed = set(), Counter()
    with transaction(engine) as session:
        context = _context(session, groups, audit_hash, validate_audit=False)
        unqualified = set()
        for (rule, security_id, raw_id, params_hash), count in context["issues"].items():
            if rule != "source_row_validation":
                unqualified.add(security_id)
            else:
                observed[(security_id, raw_id, params_hash)] += count
        for security_id, entries in per_security.items():
            relevant_expected = {key: value for key, value in expected.items() if key[0] == security_id}
            relevant_observed = {key: value for key, value in observed.items() if key[0] == security_id}
            if security_id not in unqualified and relevant_expected == relevant_observed:
                qualified.add(security_id)
        # Re-read actual archive/request/capture evidence and complete SQL receipts,
        # rather than treating a JSON completion flag as a sufficient repair proof.
        store = RawStore(Path(recovery_receipt["raw_root"]))
        for security_id in list(qualified):
            for index, group in per_security[security_id]:
                _, _, _, captures = _group(session, store, group, context)
                if any(_receipt(audit_hash, index, original, context) is None for original in captures):
                    qualified.remove(security_id)
                    break
        jobs = session.scalars(select(CollectionJob).where(CollectionJob.source == "kis", CollectionJob.domain == "prices",
            CollectionJob.status == "unsupported", CollectionJob.request["market"].astext == "KR",
            CollectionJob.checkpoint["reason"].astext == "source_row_validation_gap",
            CollectionJob.entity_id.in_(per_security)).with_for_update(skip_locked=True)).all()
        job_ids, skipped = [], 0
        for job in jobs:
            try:
                cursor = date.fromisoformat(job.checkpoint["cursor_end"])
                valid_cursor = date.fromisoformat(job.request["output_start"]) <= cursor <= date.fromisoformat(job.request["output_end"])
            except (KeyError, TypeError, ValueError):
                valid_cursor = False
            if job.entity_id not in qualified or not valid_cursor:
                skipped += 1
                continue
            previous = deepcopy(job.checkpoint)
            proof = {"audit_hash": audit_hash, "normalizer_version": KIS_NORMALIZER_VERSION,
                "recovered_issue_count": sum(value for key, value in expected.items() if key[0] == job.entity_id),
                "recovered_at": utcnow().isoformat(), "history_coverage_qualified": False}
            history = list(job.checkpoint.get("repair_history", []))
            history.append({"previous_status": job.status, "previous_checkpoint": previous, "archive_recovery": proof})
            job.checkpoint = {**job.checkpoint, "repair_history": history, "archive_recovery": proof,
                "reason": "archive_rows_recovered_resume_saved_cursor", "rejected_total": 0, "history_coverage_qualified": False}
            job.status, job.lease_owner, job.lease_until = "retry", None, None
            job_ids.append(job.id)
        session.flush()
    return {"requeued_jobs": len(job_ids), "job_ids": job_ids, "skipped_jobs": skipped, "history_coverage_qualified": False}
