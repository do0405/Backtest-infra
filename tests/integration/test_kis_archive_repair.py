from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
import gzip
import importlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from backtest_data.catalog.models import Security
from backtest_data.catalog.sources import ensure_contract
from backtest_data.db import new_id
from backtest_data.ingestion.http import ProviderResponse
from backtest_data.ingestion.jobs import stable_hash
from backtest_data.ingestion.models import CollectionJob, IngestionAttempt, RawObject
from backtest_data.ingestion.raw_store import RawStore
from backtest_data.prices.adapters.kis import KISDailyRequest
from backtest_data.prices.models import PriceObservation
from backtest_data.prices.normalize import normalize_observation
from backtest_data.prices.storage import KIS_SPEC, store_kis_prices
from backtest_data.releases.models import SelectionDecision, ValidationIssue


pytestmark = pytest.mark.integration


def repair_module():
    return importlib.import_module("backtest_data.ops.kis_repair")


def seed(db_session, tmp_path, monkeypatch):
    import backtest_data.prices.storage as module
    security = Security(market="KR", source_identifier="kis-archive-repair", name="Example", security_type="unclassified")
    db_session.add(security)
    db_session.flush()
    request = KISDailyRequest("Q530124", date(2016, 1, 1), date(2025, 2, 16))
    row = {"stck_bsop_date": "20250114", "stck_oprc": "19955", "stck_hgpr": "19955",
           "stck_lwpr": "19955", "stck_clpr": "19955", "acml_vol": "100", "acml_tr_pbmn": "1995500"}
    payload = {"rt_cd": "0", "output2": [row, row | {"stck_bsop_date": "20250113", "acml_vol": "0", "acml_tr_pbmn": "0"}]}
    content = json.dumps(payload).encode()
    captured = [datetime(2025, 2, day, tzinfo=timezone.utc) for day in (17, 18)]
    current = module.normalize_kis_bar

    def old(*args, **kwargs):
        return normalize_observation(replace(current(*args, **kwargs), no_trade_evidence=None))

    with monkeypatch.context() as patch:
        patch.setattr(module, "normalize_kis_bar", old)
        for timestamp in captured:
            result = store_kis_prices(db_session, security.id, request,
                ProviderResponse(content, 200, timestamp, "application/json"), payload, tmp_path / "raw")
            assert result["inserted"] == 1 and result["rejected"] == 1
    raw = db_session.scalar(select(RawObject).where(RawObject.content_hash == result["raw_hash"]))
    originals = db_session.scalars(select(IngestionAttempt).where(IngestionAttempt.content_hash == raw.content_hash,
        IngestionAttempt.status == "received").order_by(IngestionAttempt.captured_at)).all()
    audit = {"recovery": {"groups": [{"security_id": security.id, "raw_id": raw.id,
        "request": request.params(), "eligible_issues": 2, "remaining_issues": 0}]}}
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    return security, raw, originals, captured, audit, audit_path


def test_all_original_capture_events_are_replayed_once_with_immutable_history(db_session, tmp_path, monkeypatch):
    module = repair_module()
    security, raw, originals, captured, audit, audit_path = seed(db_session, tmp_path, monkeypatch)
    old_rows = db_session.scalars(select(PriceObservation).where(PriceObservation.security_id == security.id)).all()
    snapshots = {row.id: (row.signature, deepcopy(row.evidence)) for row in old_rows}
    old_issues = [(item.id, deepcopy(item.evidence)) for item in db_session.scalars(select(ValidationIssue))
        if item.evidence.get("security_id") == security.id]
    # A replay event can never become an additional original capture event.
    db_session.add(IngestionAttempt(source_contract_id=originals[0].source_contract_id,
        request_hash=originals[0].request_hash, content_hash=raw.content_hash, status="replayed",
        evidence=deepcopy(originals[0].evidence), captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    db_session.flush()
    first = module.replay_kis_price_audit(db_session.get_bind(), tmp_path / "raw", audit_path)
    second = module.replay_kis_price_audit(db_session.get_bind(), tmp_path / "raw", audit_path)
    assert first["status"] == "complete" and first["processed_groups"] == 1
    assert first["processed_original_captures"] == 2 and first["inserted_this_run"] == 2
    assert second["inserted_this_run"] == 0 and second["processed_original_captures"] == 2
    assert first["recovered_security_ids"] == [security.id] and first["history_coverage_qualified"] is False
    db_session.expire_all()
    rows = db_session.scalars(select(PriceObservation).where(PriceObservation.security_id == security.id,
        PriceObservation.volume == 0).order_by(PriceObservation.captured_at)).all()
    assert [item.captured_at for item in rows] == captured
    assert {item.evidence["replay_evidence"]["original_ingestion_attempt_id"] for item in rows} == {item.id for item in originals}
    assert all(item.available_at == item.captured_at and item.evidence["research_only"] for item in rows)
    for old in old_rows:
        assert (old.signature, old.evidence) == snapshots[old.id]
    assert [(item.id, item.evidence) for item in db_session.scalars(select(ValidationIssue))
        if item.evidence.get("security_id") == security.id] == old_issues
    for row in rows:
        decision = db_session.scalar(select(SelectionDecision).where(SelectionDecision.record_id == row.id))
        assert decision.recorded_at > row.captured_at


@pytest.mark.parametrize("problem", ["hash", "storage_key", "bytes", "request_flag", "extra_param",
    "attempt_params", "attempt_hash", "market", "corrupt_archive"])
def test_contradictory_archive_or_request_evidence_cannot_write_replay(db_session, tmp_path, monkeypatch, problem):
    module = repair_module()
    # Insert contradictory original metadata as a fixture. Existing immutable
    # rows must never be UPDATEd, including by a test simulating an anomaly.
    security = Security(market="US" if problem == "market" else "KR", source_identifier="kis-bad-original-proof",
        name="Example", security_type="unclassified")
    db_session.add(security)
    db_session.flush()
    request = KISDailyRequest("Q530124", date(2016, 1, 1), date(2025, 2, 16))
    content = json.dumps({"rt_cd": "0", "output2": [{"stck_bsop_date": "20250113", "stck_oprc": "19955",
        "stck_hgpr": "19955", "stck_lwpr": "19955", "stck_clpr": "19955", "acml_vol": "0", "acml_tr_pbmn": "0"}]}).encode()
    stored = RawStore(tmp_path / "raw").put(content)
    raw = RawObject(content_hash="invalid-sha" if problem == "hash" else stored.content_hash,
        storage_key="../escape.gz" if problem == "storage_key" else stored.storage_key,
        byte_count=stored.byte_count + int(problem == "bytes"))
    db_session.add(raw)
    db_session.flush()
    contract = ensure_contract(db_session, source="kis", domain="prices", market="KR", specification=KIS_SPEC)
    for index, day in enumerate((17, 18)):
        params = request.params()
        if problem == "attempt_params":
            params["FID_INPUT_DATE_2"] = "20250215"
        db_session.add(IngestionAttempt(source_contract_id=contract.id,
            request_hash="0" * 64 if problem == "attempt_hash" and index == 0 else stable_hash(request.params()),
            content_hash=raw.content_hash, status="received", captured_at=datetime(2025, 2, day, tzinfo=timezone.utc),
            evidence={"endpoint": KIS_SPEC["endpoint"], "params": params}))
        db_session.add(ValidationIssue(record_domain="price_observation", rule="source_row_validation", severity="error",
            evidence={"raw_id": raw.id, "security_id": security.id, "request": request.params(), "session": "20250113"}))
    audit = {"recovery": {"groups": [{"security_id": security.id, "raw_id": raw.id,
        "request": request.params(), "eligible_issues": 2, "remaining_issues": 0}]}}
    if problem == "request_flag":
        audit["recovery"]["groups"][0]["request"]["FID_ORG_ADJ_PRC"] = "0"
    elif problem == "extra_param":
        audit["recovery"]["groups"][0]["request"]["extra"] = "not-part-of-contract"
    elif problem == "corrupt_archive":
        (tmp_path / "raw" / raw.storage_key).write_bytes(gzip.compress(b"{}"))
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    db_session.flush()
    before = len(db_session.scalars(select(IngestionAttempt)).all())
    with pytest.raises(ValueError):
        module.replay_kis_price_audit(db_session.get_bind(), tmp_path / "raw", audit_path)
    assert len(db_session.scalars(select(IngestionAttempt)).all()) == before
    assert not db_session.scalars(select(PriceObservation).where(PriceObservation.security_id == security.id,
        PriceObservation.volume == 0)).all()


def test_tampered_group_cursor_cannot_skip_a_second_original_capture(db_session, tmp_path, monkeypatch):
    module = repair_module()
    security, raw, originals, captured, audit, audit_path = seed(db_session, tmp_path, monkeypatch)
    clock = iter([0, 0, 0, 2])
    monkeypatch.chdir(tmp_path)
    progress_path = tmp_path / "artifacts" / "progress.json"
    with monkeypatch.context() as patch:
        patch.setattr(module.time, "monotonic", lambda: next(clock))
        module.replay_kis_price_audit(db_session.get_bind(), tmp_path / "raw", audit_path,
            progress_path=progress_path, budget_seconds=1)
    saved = json.loads(progress_path.read_text(encoding="utf-8"))
    saved.update(next_group_index=1, processed_groups=1, next_attempt_index=0, original_attempt_ids=[])
    progress_path.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ValueError, match="original captures"):
        module.replay_kis_price_audit(db_session.get_bind(), tmp_path / "raw", audit_path, progress_path=progress_path)


def test_each_original_capture_rolls_back_atomically_on_storage_failure(db_session, tmp_path, monkeypatch):
    module = repair_module()
    security, raw, originals, captured, audit, audit_path = seed(db_session, tmp_path, monkeypatch)
    real = module.store_kis_prices

    def fail_after_write(*args, **kwargs):
        real(*args, **kwargs)
        raise ValueError("forced atomic replay failure")

    monkeypatch.setattr(module, "store_kis_prices", fail_after_write)
    before = len(db_session.scalars(select(IngestionAttempt)).all())
    with pytest.raises(ValueError, match="atomic replay"):
        module.replay_kis_price_audit(db_session.get_bind(), tmp_path / "raw", audit_path)
    db_session.expire_all()
    assert len(db_session.scalars(select(IngestionAttempt)).all()) == before
    assert not db_session.scalars(select(PriceObservation).where(PriceObservation.security_id == security.id,
        PriceObservation.volume == 0)).all()


def test_budget_checkpoint_resumes_the_next_original_capture(db_session, tmp_path, monkeypatch):
    module = repair_module()
    security, raw, originals, captured, audit, audit_path = seed(db_session, tmp_path, monkeypatch)
    clock = iter([0, 0, 0, 2])
    monkeypatch.chdir(tmp_path)
    progress_path = tmp_path / "artifacts" / "progress.json"
    with monkeypatch.context() as patch:
        patch.setattr(module.time, "monotonic", lambda: next(clock))
        partial = module.replay_kis_price_audit(db_session.get_bind(), tmp_path / "raw", audit_path,
            progress_path=progress_path, budget_seconds=1)
    assert partial["status"] == "partial" and partial["processed_original_captures"] == 1
    assert partial["recovered_security_ids"] == []
    complete = module.replay_kis_price_audit(db_session.get_bind(), tmp_path / "raw", audit_path, progress_path=progress_path)
    assert complete["status"] == "complete" and complete["processed_original_captures"] == 2
    assert complete["inserted"] == 2 and complete["inserted_this_run"] == 1


@pytest.mark.parametrize("problem", ["different_existing_kr_security", "wrong_eligible_count"])
def test_sql_issue_identity_and_counts_bind_audit_before_any_replay(db_session, tmp_path, monkeypatch, problem):
    module = repair_module()
    security, raw, originals, captured, audit, audit_path = seed(db_session, tmp_path, monkeypatch)
    if problem == "different_existing_kr_security":
        wrong = Security(market="KR", source_identifier="different-existing-kr", name="Wrong", security_type="unclassified")
        db_session.add(wrong)
        db_session.flush()
        audit["recovery"]["groups"][0]["security_id"] = wrong.id
    else:
        audit["recovery"]["groups"][0]["eligible_issues"] = 1
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    before = len(db_session.scalars(select(IngestionAttempt)).all())
    with pytest.raises(ValueError, match="issue"):
        module.replay_kis_price_audit(db_session.get_bind(), tmp_path / "raw", audit_path)
    assert len(db_session.scalars(select(IngestionAttempt)).all()) == before


def test_progress_cannot_write_outside_workspace_artifacts(db_session, tmp_path, monkeypatch):
    module = repair_module()
    security, raw, originals, captured, audit, audit_path = seed(db_session, tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="artifacts"):
        module.replay_kis_price_audit(db_session.get_bind(), tmp_path / "raw", audit_path,
            progress_path=Path.cwd() / "data" / "forbidden-replay-progress.json")


def test_targeted_requeue_requires_complete_receipts_and_preserves_saved_cursor(db_session, tmp_path, monkeypatch):
    module = repair_module()
    security, raw, originals, captured, audit, audit_path = seed(db_session, tmp_path, monkeypatch)
    checkpoint = {"reason": "source_row_validation_gap", "cursor_end": "2020-01-31", "rejected_total": 2,
        "source_rows_total": 1000, "custom_preserved": "value"}
    jobs = []
    for status, reason, market, cursor in [("unsupported", "source_row_validation_gap", "KR", "2020-01-31"),
        ("unsupported", "403", "KR", "2020-01-31"), ("failed", "source_row_validation_gap", "KR", "2020-01-31"),
        ("unsupported", "source_row_validation_gap", "US", "2020-01-31"),
        ("unsupported", "source_row_validation_gap", "KR", None)]:
        job_id = new_id()
        prior = {**checkpoint, "reason": reason}
        if cursor is None:
            prior.pop("cursor_end")
        job = CollectionJob(id=job_id, identity_hash=stable_hash(job_id), source="kis", domain="prices", entity_id=security.id,
            request={"market": market, "symbol": "Q530124", "output_start": "2016-01-01", "output_end": "2025-02-16"},
            status=status, checkpoint=prior, attempts=9)
        db_session.add(job)
        jobs.append(job)
    db_session.flush()
    receipt = module.replay_kis_price_audit(db_session.get_bind(), tmp_path / "raw", audit_path)
    result = module.requeue_recovered_kis_jobs(db_session.get_bind(), audit_path, receipt)
    assert result["requeued_jobs"] == 1 and result["job_ids"] == [jobs[0].id]
    db_session.expire_all()
    recovered = jobs[0]
    assert recovered.status == "retry" and recovered.attempts == 9
    assert recovered.checkpoint["cursor_end"] == "2020-01-31"
    assert recovered.checkpoint["source_rows_total"] == 1000 and recovered.checkpoint["rejected_total"] == 0
    assert recovered.checkpoint["custom_preserved"] == "value"
    assert recovered.checkpoint["repair_history"][-1]["previous_checkpoint"] == checkpoint
    assert recovered.checkpoint["history_coverage_qualified"] is False
    assert [job.status for job in jobs[1:]] == ["unsupported", "failed", "unsupported", "unsupported"]


def test_requeue_cannot_clear_new_uncovered_validation_issue(db_session, tmp_path, monkeypatch):
    module = repair_module()
    security, raw, originals, captured, audit, audit_path = seed(db_session, tmp_path, monkeypatch)
    receipt = module.replay_kis_price_audit(db_session.get_bind(), tmp_path / "raw", audit_path)
    job_id = new_id()
    db_session.add(CollectionJob(id=job_id, identity_hash=stable_hash(job_id), source="kis", domain="prices", entity_id=security.id,
        request={"market": "KR", "symbol": "Q530124", "output_start": "2016-01-01", "output_end": "2025-02-16"},
        status="unsupported", checkpoint={"reason": "source_row_validation_gap", "cursor_end": "2020-01-31", "rejected_total": 3}))
    db_session.add(ValidationIssue(record_domain="price_observation", rule="source_row_validation", severity="error",
        evidence={"raw_id": raw.id, "security_id": security.id, "session": "20250112", "request": originals[0].evidence["params"]}))
    db_session.flush()
    result = module.requeue_recovered_kis_jobs(db_session.get_bind(), audit_path, receipt)
    assert result["requeued_jobs"] == 0
    job = db_session.get(CollectionJob, job_id)
    db_session.refresh(job)
    assert job.status == "unsupported" and job.checkpoint["rejected_total"] == 3
