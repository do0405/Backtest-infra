from datetime import date, datetime, timezone
from copy import deepcopy
from dataclasses import replace
import json

import pytest
from sqlalchemy import select

from backtest_data.catalog.models import Security
from backtest_data.ingestion.http import ProviderResponse
from backtest_data.ingestion.models import RawObject, IngestionAttempt
from backtest_data.ingestion.raw_store import RawStore
from backtest_data.prices.adapters.kis import KISDailyRequest
from backtest_data.prices.models import PriceObservation
from backtest_data.prices.storage import store_kis_prices
from backtest_data.prices.normalize import normalize_observation
from backtest_data.releases.models import SelectionDecision, ValidationIssue


pytestmark = pytest.mark.integration


def bar(day="20250113", **changes):
    return {"stck_bsop_date": day, "stck_oprc": "19955", "stck_hgpr": "19955",
            "stck_lwpr": "19955", "stck_clpr": "19955", "acml_vol": "100", "acml_tr_pbmn": "1995500"} | changes


def store(db_session, tmp_path, rows, *, start=date(2016, 1, 1), end=date(2025, 2, 16)):
    security = Security(market="KR", source_identifier="kis-domestic-validation", security_type="unclassified", name="Example")
    db_session.add(security)
    db_session.flush()
    payload = {"rt_cd": "0", "output2": rows}
    response = ProviderResponse(json.dumps(payload).encode(), 200, datetime(2026, 10, 9, tzinfo=timezone.utc), "application/json")
    result = store_kis_prices(db_session, security.id, KISDailyRequest("Q530124", start, end), response, payload, tmp_path)
    observations = db_session.scalars(select(PriceObservation).where(PriceObservation.security_id == security.id)).all()
    issues = [item for item in db_session.scalars(select(ValidationIssue)) if item.evidence.get("security_id") == security.id]
    return result, observations, issues


def test_supplier_zero_transaction_quote_is_raw_anchored_research_evidence(db_session, tmp_path):
    rows = [bar(acml_vol="0", acml_tr_pbmn="0")]
    result, observations, issues = store(db_session, tmp_path, rows)
    assert result["inserted"] == 1 and result["rejected"] == 0 and not issues
    observation, = observations
    assert observation.evidence["supplier_reported_zero_transactions"] is True
    assert observation.evidence["no_trade_status_verified"] is False
    assert observation.evidence["research_only"] is True
    assert observation.evidence["matched_vwap_eligible"] is False
    proof = observation.evidence["supplier_zero_transaction_evidence"]
    assert proof == {"raw_id": observation.raw_id, "session": "20250113", "acml_vol": "0", "acml_tr_pbmn": "0",
                     "stck_oprc": "19955", "stck_hgpr": "19955", "stck_lwpr": "19955", "stck_clpr": "19955"}
    raw = db_session.get(RawObject, observation.raw_id)
    assert json.loads(RawStore(tmp_path).read(raw.storage_key))["output2"] == rows
    decision = db_session.scalar(select(SelectionDecision).where(SelectionDecision.record_id == observation.id))
    assert decision.rule_version == "kis_supplier_zero_transaction_structure_v1"
    assert decision.evidence_grade == "supplier_reported_unverified"
    assert "research" in decision.reason.lower()
    assert observation.available_at == observation.captured_at


def test_rejected_source_row_retains_precise_failure_and_index(db_session, tmp_path):
    result, observations, issues = store(db_session, tmp_path, [bar(acml_vol="0", acml_tr_pbmn="1")])
    assert not observations and result["rejected"] == 1
    issue, = issues
    assert issue.evidence["row_index"] == 0
    assert issue.evidence["error_type"] == "ValueError"
    assert "zero_volume_notional" in issue.evidence["reason"]
    assert result["rejection_reason_counts"] == {issue.evidence["reason"]: 1}


def test_start_boundary_overrun_is_archived_and_excluded_without_rejecting(db_session, tmp_path):
    result, observations, issues = store(db_session, tmp_path, [bar("20250113"), bar("20241231")], start=date(2025, 1, 1))
    assert result["inserted"] == 1 and result["rejected"] == 0 and not issues
    assert [item.session for item in observations] == [date(2025, 1, 13)]
    assert result["boundary_excluded_count"] == 1 and result["boundary_reached"] is True
    assert result["source_row_count"] == 2 and result["cursor_earliest_session"] == "2024-12-31"
    assert result["pagination_valid"] is True and result["traversal_complete"] is True


@pytest.mark.parametrize("rows", [
    [bar("20250113"), bar("20250217")],
    [bar("20250113"), bar("20250113")],
    [bar("20250112"), bar("20250113")],
    [bar("not-a-date")], [bar("")], [None], [{"stck_bsop_date": ""}],
])
def test_malformed_or_nonprogressing_pages_cannot_insert_or_complete(db_session, tmp_path, rows):
    result, observations, issues = store(db_session, tmp_path, rows)
    assert not observations and issues
    assert result["pagination_valid"] is False and result["traversal_complete"] is False
    assert result["normalized_row_coverage_complete"] is False


@pytest.mark.parametrize("rows", [[], [{key: "" for key in bar()}]])
def test_empty_or_blank_provider_page_is_exhaustion_without_history_qualification(db_session, tmp_path, rows):
    result, observations, issues = store(db_session, tmp_path, rows)
    assert not observations and not issues
    assert result["source_exhausted"] is True and result["traversal_complete"] is True
    assert result["history_coverage_qualified"] is False


def test_short_page_above_start_requires_explicit_older_exhaustion_probe(db_session, tmp_path):
    result, observations, issues = store(db_session, tmp_path, [bar()])
    assert len(observations) == 1 and not issues
    assert result["source_exhausted"] is False and result["traversal_complete"] is False
    assert result["cursor_earliest_session"] == "2025-01-13"


def test_original_capture_archive_replay_appends_missing_row_once_without_mutating_history(db_session, tmp_path, monkeypatch):
    import backtest_data.prices.storage as module
    security = Security(market="KR", source_identifier="kis-original-capture-replay", security_type="unclassified", name="Example")
    db_session.add(security)
    db_session.flush()
    request = KISDailyRequest("Q530124", date(2016, 1, 1), date(2025, 2, 16))
    captured = datetime(2025, 2, 17, tzinfo=timezone.utc)
    payload = {"rt_cd": "0", "output2": [bar("20250114"), bar(acml_vol="0", acml_tr_pbmn="0")]}
    response = ProviderResponse(json.dumps(payload).encode(), 200, captured, "application/json")
    current_normalizer = module.normalize_kis_bar

    def historical_normalizer(*args, **kwargs):
        # Reproduce the old guard with actual mapped values and SQL writes.
        result = current_normalizer(*args, **kwargs)
        return normalize_observation(replace(result, no_trade_evidence=None))

    with monkeypatch.context() as patch:
        patch.setattr(module, "normalize_kis_bar", historical_normalizer)
        original = store_kis_prices(db_session, security.id, request, response, payload, tmp_path)
    assert original["inserted"] == 1 and original["rejected"] == 1
    old_row = db_session.scalar(select(PriceObservation).where(PriceObservation.security_id == security.id))
    old_evidence, old_signature = deepcopy(old_row.evidence), old_row.signature
    old_decision = db_session.scalar(select(SelectionDecision).where(SelectionDecision.record_id == old_row.id))
    old_decision_snapshot = (old_decision.id, old_decision.rule_version, old_decision.recorded_at)
    old_attempt = db_session.scalar(select(IngestionAttempt).where(IngestionAttempt.content_hash == original["raw_hash"]))
    old_attempt_evidence = deepcopy(old_attempt.evidence)
    old_issue = next(item for item in db_session.scalars(select(ValidationIssue)) if item.evidence.get("security_id") == security.id)
    old_issue_evidence = deepcopy(old_issue.evidence)
    proof = {"original_ingestion_attempt_id": old_attempt.id, "recovery_receipt": "fixture-archive-recovery"}
    before_replay = module.utcnow()

    first = store_kis_prices(db_session, security.id, request, response, payload, tmp_path, replay_evidence=proof)
    second = store_kis_prices(db_session, security.id, request, response, payload, tmp_path, replay_evidence=proof)
    assert first["inserted"] == 1 and len(first["inserted_record_ids"]) == 1
    assert second["inserted"] == 0 and second["inserted_record_ids"] == []
    new_row = db_session.get(PriceObservation, first["inserted_record_ids"][0])
    assert new_row.session == date(2025, 1, 13)
    assert new_row.available_at == new_row.captured_at == captured
    assert new_row.evidence["replay_evidence"] == proof
    assert new_row.evidence["normalizer_version"] == "kis_domestic_source_quote_v2"
    new_decision = db_session.scalar(select(SelectionDecision).where(SelectionDecision.record_id == new_row.id))
    assert new_decision.recorded_at >= before_replay > captured
    for result in (first, second):
        attempt = db_session.get(IngestionAttempt, result["ingestion_attempt_id"])
        assert attempt.status == "replayed" and attempt.captured_at == captured
        assert attempt.evidence["replay_evidence"] == proof
        assert attempt.evidence["normalizer_version"] == new_row.evidence["normalizer_version"]
    for old in (old_row, old_attempt, old_issue, old_decision):
        db_session.refresh(old)
    assert old_row.evidence == old_evidence and old_row.signature == old_signature
    assert old_attempt.status == "received" and old_attempt.evidence == old_attempt_evidence
    assert old_issue.evidence == old_issue_evidence
    assert (old_decision.id, old_decision.rule_version, old_decision.recorded_at) == old_decision_snapshot
    assert len(db_session.scalars(select(PriceObservation).where(PriceObservation.security_id == security.id)).all()) == 2


@pytest.mark.parametrize("proof", [{}, [], "attempt", {"original_ingestion_attempt_id": ""},
    {"original_ingestion_attempt_id": "  "}, {"original_ingestion_attempt_id": None},
    {"original_ingestion_attempt_id": True}])
def test_invalid_archive_replay_proof_fails_before_new_evidence_is_written(db_session, tmp_path, proof):
    security = Security(market="KR", source_identifier="kis-invalid-replay-proof", security_type="unclassified", name="Example")
    db_session.add(security)
    db_session.flush()
    payload = {"rt_cd": "0", "output2": [bar()]}
    response = ProviderResponse(json.dumps(payload).encode(), 200, datetime(2025, 2, 17, tzinfo=timezone.utc), "application/json")
    attempts_before = len(db_session.scalars(select(IngestionAttempt)).all())
    with pytest.raises(ValueError, match="original_ingestion_attempt_id"):
        store_kis_prices(db_session, security.id, KISDailyRequest("Q530124", date(2016, 1, 1), date(2025, 2, 16)),
            response, payload, tmp_path, replay_evidence=proof)
    assert len(db_session.scalars(select(IngestionAttempt)).all()) == attempts_before
    assert not db_session.scalars(select(PriceObservation).where(PriceObservation.security_id == security.id)).all()
    assert not list(tmp_path.rglob("*.gz"))
