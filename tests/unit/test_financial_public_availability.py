"""Document-bound publication evidence must never invent an earlier version."""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest


UTC = timezone.utc
CAPTURE = datetime(2026, 10, 1, tzinfo=UTC)
PUBLIC_BOUND = datetime(2024, 3, 2, 5, tzinfo=UTC)


def fixture_document(*, source="sec", revision_kind="original"):
    filing = SimpleNamespace(id="F", issuer_id="I", source=source, accession="A",
        available_at=CAPTURE, captured_at=CAPTURE, published_at=None, evidence={})
    document = SimpleNamespace(id="D", filing_id="F", content_hash="a" * 64,
        revision_kind=revision_kind, available_at=CAPTURE, captured_at=CAPTURE,
        extraction_complete=True, inventory={"counts": {"numeric_nodes": 2, "extracted_nodes": 2,
            "deduplicated_nodes": 0, "unsupported_nodes": 0, "rejected_nodes": 0},
            "source_format_complete": True, "issues": []})
    return document, filing


def certificate(document, filing, *, status="qualified", id="E", public_day=date(2024, 3, 1),
                bound=PUBLIC_BOUND, supersedes=None):
    source = filing.source
    quarter = (public_day.month - 1) // 3 + 1
    proof = {"kind": "sec_dated_feed_original" if source == "sec" else "dart_receipt_original",
        "public_day": public_day.isoformat(), "issuer_id": filing.issuer_id,
        "accession": filing.accession, "source_url": f"https://www.sec.gov/Archives/edgar/Feed/{public_day.year}/QTR{quarter}/{public_day.strftime('%Y%m%d')}.nc.tar.gz"
            if source == "sec" else "https://opendart.fss.or.kr/api/list.json",
        "source_body_hash": "b" * 64, "original_body_hash": "c" * 64,
        "membership_verified": True, "original_version_verified": True}
    if source == "sec":
        proof.update(member_hash="c" * 64, member_name="A/original.xml",
            archive_url=proof["source_url"])
    else:
        proof["receipt_specific_original"] = True
        proof["original_url"] = "https://opendart.fss.or.kr/api/fnlttXbrl.xml"
        proof["request_parameters"] = {"rcept_no": filing.accession}
    if status == "withdrawal":
        proof.update(event_kind="withdrawal", withdrawal_verified=True)
    evidence = {"binding": {"document_id": document.id, "filing_id": filing.id,
        "issuer_id": filing.issuer_id, "source": source, "accession": filing.accession,
        "document_hash": document.content_hash}, "proof": proof,
        "numeric_binding": {"sql_digest": "d" * 64, "original_digest": "d" * 64,
            "numeric_nodes": 2, "extracted_nodes": 2, "deduplicated_nodes": 0, "fact_count": 2,
            "complete": True, "unsupported_nodes": 0, "ambiguous_nodes": 0}}
    return SimpleNamespace(id=id, document_id=document.id, filing_id=filing.id,
        issuer_id=filing.issuer_id, source=source, accession=filing.accession,
        document_hash=document.content_hash, status=status, public_day=public_day,
        effective_available_at=bound, precision="public_day", timezone="America/New_York"
            if source == "sec" else "Asia/Seoul", policy_version="financial_public_pit_v1",
        captured_at=CAPTURE, recorded_at=CAPTURE, assertion_hash=id * 64,
        supersedes_assertion_hash=supersedes, evidence=evidence)


def resolve(document, filing, evidence, *, as_of=PUBLIC_BOUND, mode="reconstructed_public"):
    from backtest_data.fundamentals.public_availability import resolve_document_availability
    return resolve_document_availability(document, filing, evidence, history_mode=mode, as_of=as_of)


def test_verified_original_is_unavailable_before_its_public_day_bound():
    document, filing = fixture_document()
    row = certificate(document, filing)
    before = resolve(document, filing, [row], as_of=datetime(2024, 3, 2, 4, 59, tzinfo=UTC))
    after = resolve(document, filing, [row])
    assert before["status"] == "unavailable"
    assert before["reason"] == "before_public_availability"
    assert after["status"] == "qualified"
    assert after["effective_available_at"] == PUBLIC_BOUND
    assert after["evidence_ids"] == ["E"]
    assert after["precision"] == "public_day"
    assert after["policy_version"] == "financial_public_pit_v1"
    assert after["withdrawn"] is False
    assert after["verified_public_bound"] == PUBLIC_BOUND


def test_unverified_history_uses_capture_and_never_period_end_or_filing_date():
    document, filing = fixture_document()
    filing.published_at = datetime(2024, 3, 1, tzinfo=UTC)
    filing.evidence = {"filingDate": "2024-03-01", "acceptanceDateTime": "2024-03-01T12:00:00Z"}
    document.inventory["period_end"] = "2023-12-31"
    before = resolve(document, filing, [], as_of=PUBLIC_BOUND)
    after = resolve(document, filing, [], as_of=CAPTURE)
    assert before["status"] == "unavailable"
    assert after["status"] == "capture_only"
    assert after["effective_available_at"] == CAPTURE
    assert after["evidence_ids"] == []
    assert after["verified_public_bound"] is None


def test_observed_history_keeps_document_capture_and_event_recording_cutoffs():
    document, filing = fixture_document()
    row = certificate(document, filing)
    row.recorded_at = datetime(2026, 10, 3, tzinfo=UTC)
    assert resolve(document, filing, [row], mode="observed_system")["status"] == "unavailable"
    at_capture = resolve(document, filing, [row], mode="observed_system", as_of=CAPTURE)
    assert at_capture["effective_available_at"] == CAPTURE
    assert at_capture["evidence_ids"] == []
    after_record = resolve(document, filing, [row], mode="observed_system", as_of=row.recorded_at)
    assert after_record["effective_available_at"] == CAPTURE
    assert after_record["evidence_ids"] == ["E"]
    assert after_record["verified_public_bound"] == PUBLIC_BOUND


def test_unavailable_retrieval_does_not_revoke_a_qualified_original():
    document, filing = fixture_document()
    good = certificate(document, filing)
    attempt = certificate(document, filing, status="unavailable", id="U")
    attempt.evidence = {"reason": "source_unavailable"}
    assert resolve(document, filing, [good, attempt])["status"] == "qualified"
    assert resolve(document, filing, [good, attempt])["evidence_ids"] == ["E"]


def test_rejection_revokes_only_its_exact_target_and_respects_observed_cutoff():
    document, filing = fixture_document()
    good = certificate(document, filing)
    unrelated = certificate(document, filing, status="rejected", id="R", supersedes="x" * 64)
    assert resolve(document, filing, [good, unrelated])["status"] == "qualified"
    rejection = certificate(document, filing, status="rejected", id="R", supersedes=good.assertion_hash)
    rejection.captured_at = rejection.recorded_at = datetime(2026, 10, 4, tzinfo=UTC)
    result = resolve(document, filing, [good, rejection], as_of=CAPTURE)
    assert result["status"] == "capture_only"
    assert result["reason"] == "publication_assertion_rejected"
    assert resolve(document, filing, [good, rejection], mode="observed_system", as_of=CAPTURE)["status"] == "qualified"


def test_withdrawal_applies_only_after_its_own_verified_public_day():
    document, filing = fixture_document()
    good = certificate(document, filing)
    bound = datetime(2024, 3, 6, 5, tzinfo=UTC)
    withdrawal = certificate(document, filing, status="withdrawal", id="W", supersedes=good.assertion_hash,
        public_day=date(2024, 3, 5), bound=bound)
    assert resolve(document, filing, [good, withdrawal])["status"] == "qualified"
    result = resolve(document, filing, [good, withdrawal], as_of=bound)
    assert result["status"] == "unavailable"
    assert result["reason"] == "publication_withdrawn"
    assert result["withdrawn"] is True
    assert result["evidence_ids"] == ["E", "W"]
    assert result["verified_public_bound"] == PUBLIC_BOUND


def test_withdrawal_before_its_target_original_publication_is_invalid():
    document, filing = fixture_document()
    good = certificate(document, filing)
    withdrawal = certificate(document, filing, status="withdrawal", id="W", supersedes=good.assertion_hash,
        public_day=date(2024, 2, 1), bound=datetime(2024, 2, 2, 5, tzinfo=UTC))
    assert resolve(document, filing, [good, withdrawal])["status"] == "qualified"


def test_amendment_is_unavailable_until_its_own_public_bound():
    document, filing = fixture_document(revision_kind="amendment")
    amendment = certificate(document, filing, public_day=date(2024, 4, 1),
        bound=datetime(2024, 4, 2, 4, tzinfo=UTC))
    assert resolve(document, filing, [amendment])["status"] == "unavailable"
    assert resolve(document, filing, [amendment], as_of=amendment.effective_available_at)["status"] == "qualified"


def test_repeated_proof_for_same_immutable_version_uses_earliest_verified_bound():
    document, filing = fixture_document()
    first = certificate(document, filing)
    second = certificate(document, filing, id="S", public_day=date(2024, 3, 3),
        bound=datetime(2024, 3, 4, 5, tzinfo=UTC))
    result = resolve(document, filing, [first, second], as_of=CAPTURE)
    assert result["status"] == "qualified"
    assert result["effective_available_at"] == PUBLIC_BOUND
    assert resolve(document, filing, [first])["status"] == "qualified"
    first.evidence["proof"]["version_order_ambiguous"] = True
    assert resolve(document, filing, [first], as_of=CAPTURE)["status"] == "capture_only"


@pytest.mark.parametrize("field,value", [("document_id", "OTHER"), ("filing_id", "OTHER"),
    ("issuer_id", "OTHER"), ("accession", "OTHER"), ("source", "opendart"),
    ("document_hash", "f" * 64), ("policy_version", "unrecognized_v99"),
    ("precision", "exact_time"), ("timezone", "UTC")])
def test_wrong_row_binding_or_policy_never_qualifies(field, value):
    document, filing = fixture_document()
    row = certificate(document, filing)
    setattr(row, field, value)
    assert resolve(document, filing, [row], as_of=CAPTURE)["status"] == "capture_only"


@pytest.mark.parametrize("section,field,value", [
    ("binding", "issuer_id", "OTHER"), ("binding", "accession", "OTHER"),
    ("binding", "document_hash", "f" * 64), ("proof", "issuer_id", "OTHER"),
    ("proof", "accession", "OTHER"), ("proof", "kind", "sec_acceptance_datetime"),
    ("proof", "membership_verified", False), ("proof", "original_version_verified", False),
    ("proof", "source_url", "https://sec.gov.example/Archives/20240301"),
    ("proof", "public_day", "2023-12-31"), ("proof", "member_hash", "e" * 64),
    ("numeric_binding", "original_digest", "e" * 64), ("numeric_binding", "numeric_nodes", 1),
    ("numeric_binding", "complete", False), ("numeric_binding", "unsupported_nodes", 1),
    ("numeric_binding", "ambiguous_nodes", 1), ("numeric_binding", "fact_count", 1)])
def test_invalid_certificate_never_qualifies(section, field, value):
    document, filing = fixture_document()
    row = certificate(document, filing)
    row.evidence[section][field] = value
    assert resolve(document, filing, [row], as_of=CAPTURE)["status"] == "capture_only"


def test_strict_public_day_and_dst_boundary_and_missing_trading_calendar():
    from backtest_data.fundamentals.public_availability import conservative_public_bound
    assert conservative_public_bound(date(2024, 3, 10), "America/New_York") == datetime(2024, 3, 11, 4, tzinfo=UTC)
    assert conservative_public_bound(date(2024, 11, 3), "America/New_York") == datetime(2024, 11, 4, 5, tzinfo=UTC)
    assert conservative_public_bound(date(2024, 3, 1), "Asia/Seoul") == datetime(2024, 3, 1, 15, tzinfo=UTC)
    with pytest.raises(ValueError, match="public_day"):
        conservative_public_bound(datetime(2024, 3, 1, tzinfo=UTC), "America/New_York")
    with pytest.raises(ValueError, match="public_day"):
        conservative_public_bound(date.max, "America/New_York")
    document, filing = fixture_document()
    row = certificate(document, filing)
    row.effective_available_at = datetime(2024, 3, 1, 5, tzinfo=UTC)
    assert resolve(document, filing, [row], as_of=CAPTURE)["status"] == "capture_only"
    row.effective_available_at = PUBLIC_BOUND
    assert resolve(document, filing, [row])["reason"] == "conservative_public_day_no_verified_session_calendar"


def test_latest_json_alone_never_proves_dart_receipt_original():
    document, filing = fixture_document(source="opendart")
    row = certificate(document, filing, bound=datetime(2024, 3, 1, 15, tzinfo=UTC))
    assert resolve(document, filing, [row])["status"] == "qualified"
    row.evidence["proof"]["receipt_specific_original"] = False
    assert resolve(document, filing, [row], as_of=CAPTURE)["status"] == "capture_only"
    row.evidence["proof"]["kind"] = "dart_latest_json"
    assert resolve(document, filing, [row], as_of=CAPTURE)["status"] == "capture_only"


def test_unsupported_history_mode_and_naive_cutoff_are_rejected():
    document, filing = fixture_document()
    with pytest.raises(ValueError, match="history_mode"):
        resolve(document, filing, [], mode="latest")
    with pytest.raises(ValueError, match="as_of"):
        resolve(document, filing, [], as_of=datetime(2024, 3, 1))


@pytest.mark.parametrize("field,value", [("fact_count", None), ("fact_count", "2"),
    ("numeric_nodes", True), ("extracted_nodes", None), ("deduplicated_nodes", "0")])
def test_malformed_inventory_counters_fail_closed(field, value):
    document, filing = fixture_document()
    row = certificate(document, filing)
    row.evidence["numeric_binding"][field] = value
    assert resolve(document, filing, [row], as_of=CAPTURE)["status"] == "capture_only"


def test_missing_document_inventory_counters_fail_closed():
    document, filing = fixture_document()
    row = certificate(document, filing)
    del document.inventory["counts"]["deduplicated_nodes"]
    assert resolve(document, filing, [row], as_of=CAPTURE)["status"] == "capture_only"


def test_late_observed_original_retains_earlier_public_version_order_than_amendment():
    original, original_filing = fixture_document()
    late = datetime(2026, 10, 5, tzinfo=UTC)
    original.available_at = original.captured_at = late
    original_filing.available_at = original_filing.captured_at = late
    first = certificate(original, original_filing)
    first.captured_at = first.recorded_at = late
    amendment, amendment_filing = fixture_document(revision_kind="amendment")
    amendment.id = "AMEND"
    amendment.filing_id = amendment_filing.id = "AMEND-FILING"
    public_bound = datetime(2024, 4, 2, 4, tzinfo=UTC)
    correction = certificate(amendment, amendment_filing, public_day=date(2024, 4, 1), bound=public_bound)
    old = resolve(original, original_filing, [first], mode="observed_system", as_of=late)
    new = resolve(amendment, amendment_filing, [correction], mode="observed_system", as_of=late)
    assert old["status"] == new["status"] == "qualified"
    assert old["effective_available_at"] > new["effective_available_at"]
    assert old["verified_public_bound"] < new["verified_public_bound"]


@pytest.mark.parametrize("field,url", [
    ("archive_url", "https://www.sec.gov/Archives/edgar/Feed/2024/QTR1/20240302.nc.tar.gz"),
    ("archive_url", "https://www.sec.gov/Archives/edgar/Feed/2024/QTR2/20240301.nc.tar.gz"),
    ("archive_url", "https://www.sec.gov/Archives/edgar/data/1/original.xml"),
    ("source_url", "https://www.sec.gov/Archives/edgar/Feed/2024/QTR1/20240302.nc.tar.gz"),
    ("source_url", "https://www.sec.gov/Archives/edgar/data/1/original.xml"),
    ("source_url", "https://[malformed"),
    ("source_url", "https://www.sec.gov:invalid/Archives/edgar/Feed/2024/QTR1/20240301.nc.tar.gz")])
def test_sec_certificate_requires_exact_official_distribution_day_and_path(field, url):
    document, filing = fixture_document()
    row = certificate(document, filing)
    row.evidence["proof"][field] = url
    assert resolve(document, filing, [row], as_of=CAPTURE)["status"] == "capture_only"


@pytest.mark.parametrize("field,value", [
    ("original_url", "https://dart.fss.or.kr/dsaf001/main.do"),
    ("original_url", "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"),
    ("request_parameters", {"rcept_no": "OTHER"}), ("request_parameters", {}),
    ("request_parameters", None)])
def test_dart_certificate_requires_receipt_original_endpoint_and_exact_requested_receipt(field, value):
    document, filing = fixture_document(source="opendart")
    row = certificate(document, filing, bound=datetime(2024, 3, 1, 15, tzinfo=UTC))
    row.evidence["proof"][field] = value
    assert resolve(document, filing, [row], as_of=CAPTURE)["status"] == "capture_only"
