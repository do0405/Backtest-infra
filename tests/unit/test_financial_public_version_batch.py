"""Bounded document batches preserve their original audit scope and physical clocks."""
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from test_financial_public_version_sources import (
    ACCESSION, DAY, FEED_URL, LIST_URL, RECEIPT, DART_URL, complete_filing,
    instance, public_list, sql_document, tar_archive, zip_archive,
)
import tarfile


CAPTURE = datetime(2026, 10, 9, tzinfo=timezone.utc)
WATERMARK = datetime(2026, 10, 8, tzinfo=timezone.utc)
PAST = datetime(2026, 10, 1, tzinfo=timezone.utc)


class PureSession:
    @contextmanager
    def begin_nested(self):
        yield self


def target(*, identifier="D", source="sec", metadata=True, preflight_reason=None):
    from backtest_data.fundamentals.public_version_batch import PublicationTarget
    body = instance() if source == "sec" else instance(entity="00123456", scheme="opendart.corp_code")
    document, filing, facts = sql_document(body, source=source,
        form="10-K" if source == "sec" else "사업보고서 (2023.12)")
    document.id = identifier
    document.captured_at = document.available_at = PAST
    filing.captured_at = filing.available_at = PAST
    filing.evidence = {}
    for fact in facts:
        fact.document_id = identifier
        fact.captured_at = fact.extracted_at = fact.first_seen_at = PAST
    issuer = SimpleNamespace(id="I", source_identifier="CIK0000320193" if source == "sec" else "DART:00123456")
    request = {"issuer_id": "I", "filing": {"accessionNumber": ACCESSION, "form": "10-K", "filingDate": "2024-03-01"}}
    if source == "opendart":
        request = {"issuer_id": "I", "filing": {"rcept_no": RECEIPT, "rcept_dt": "20240301",
            "corp_code": "00123456", "report_nm": filing.form}, "reprt_code": "11011"}
        document.inventory["source_request_metadata"] = {"rcept_no": RECEIPT, "reprt_code": "11011"}
    return PublicationTarget(document=document, filing=filing, issuer=issuer, sql_facts=tuple(facts),
        job_metadata=(request,) if metadata else (), existing_evidence=(), preflight_reason=preflight_reason)


def appender(storage):
    def append(_session, **kwargs):
        storage.append(kwargs)
        return SimpleNamespace(id="E" + str(len(storage)))
    return append


def test_sec_batch_requests_only_dated_distributions_and_append_verifier_certificate():
    from backtest_data.fundamentals.public_version_batch import run_publication_batch
    requests, storage = [], []
    def fetch(request):
        requests.append(request)
        return SimpleNamespace(content=tar_archive([(ACCESSION + ".nc", complete_filing(instance()), tarfile.REGTYPE)]), captured_at=CAPTURE)
    item = target()
    result = run_publication_batch(PureSession(), targets=[item], watermark=WATERMARK,
        fetch=fetch, now=lambda: CAPTURE, append=appender(storage), original_target_count=100)
    assert requests[0].url == FEED_URL and requests[0].source == "sec"
    assert result["original_target_count"] == 100 and result["processed_count"] == 1
    assert result["qualified_count"] == 1 and result["next_cursor"] == "D"
    assert storage[0]["public_day"] == DAY and storage[0]["captured_at"] == CAPTURE
    assert storage[0]["status"] == "qualified"
    assert item.document.captured_at == PAST and all(row.first_seen_at == PAST for row in item.sql_facts)


def test_missing_filing_date_is_unavailable_and_never_uses_period_end_or_acceptance():
    from backtest_data.fundamentals.public_version_batch import run_publication_batch
    storage = []
    result = run_publication_batch(PureSession(), targets=[target(metadata=False)], watermark=WATERMARK,
        fetch=lambda *_args: pytest.fail("no dated public archive plan"), now=lambda: CAPTURE,
        append=appender(storage), original_target_count=100)
    assert result["unavailable_count"] == 1
    assert storage[0]["status"] == "unavailable"
    assert "filing_metadata_missing" in storage[0]["evidence"]["reason"]
    assert result["original_target_count"] == 100


def test_facts_after_audit_watermark_are_not_trimmed_or_historically_qualified():
    from backtest_data.fundamentals.public_version_batch import run_publication_batch
    item, storage = target(), []
    item.sql_facts[-1].extracted_at = CAPTURE
    result = run_publication_batch(PureSession(), targets=[item], watermark=WATERMARK,
        fetch=lambda *_args: pytest.fail("incomplete watermark document"), now=lambda: CAPTURE,
        append=appender(storage))
    assert result["unavailable_count"] == 1 and len(item.sql_facts) == 2
    assert storage[0]["evidence"]["reason"] == "watermark_incomplete_document"


def test_bounded_document_limit_is_explicit_gap_without_clipped_inventory():
    from backtest_data.fundamentals.public_version_batch import run_publication_batch
    storage = []
    result = run_publication_batch(PureSession(), targets=[target(preflight_reason="document_inventory_limit")], watermark=WATERMARK,
        fetch=lambda *_args: pytest.fail("oversized document"), now=lambda: CAPTURE, append=appender(storage))
    assert result["results"][0]["reason"] == "document_inventory_limit"


def test_transport_error_is_sanitized_and_cached_without_revoking_existing_history():
    from backtest_data.fundamentals.public_version_batch import run_publication_batch
    from backtest_data.ingestion.http import ProviderError
    storage, calls = [], []
    def fetch(request):
        calls.append(request)
        raise ProviderError("sec", "403")
    result = run_publication_batch(PureSession(), targets=[target(identifier="A"), target(identifier="B")],
        watermark=WATERMARK, fetch=fetch, now=lambda: CAPTURE, append=appender(storage))
    assert result["unavailable_count"] == 2 and len(calls) == 2  # Feed then Oldloads, cached across documents.
    assert all(row["status"] == "unavailable" for row in storage)
    assert all(row["supersedes_assertion_hash"] is None for row in storage)
    assert result["next_cursor"] == "B"


def test_exception_text_with_credentials_never_enters_batch_evidence():
    from backtest_data.fundamentals.public_version_batch import run_publication_batch
    storage = []
    def fetch(_request):
        raise RuntimeError("api_key=top-secret")
    result = run_publication_batch(PureSession(), targets=[target()], watermark=WATERMARK,
        fetch=fetch, now=lambda: CAPTURE, append=appender(storage))
    assert "top-secret" not in repr(result) + repr(storage)
    assert result["unavailable_count"] == 1


def test_sec_fallback_oldloads_keeps_same_date_and_qualifies_only_actual_body():
    from backtest_data.fundamentals.public_version_batch import run_publication_batch
    from backtest_data.ingestion.http import ProviderError
    calls, storage = [], []
    def fetch(request):
        calls.append(request.url)
        if "/Feed/" in request.url:
            raise ProviderError("sec", "403")
        return tar_archive([(ACCESSION + ".nc", complete_filing(instance()), tarfile.REGTYPE)])
    result = run_publication_batch(PureSession(), targets=[target()], watermark=WATERMARK,
        fetch=fetch, now=lambda: CAPTURE, append=appender(storage))
    assert result["qualified_count"] == 1 and len(calls) == 2
    assert storage[0]["evidence"]["proof"]["archive_url"].replace("Oldloads", "Feed") == FEED_URL


def test_dart_batch_passes_specific_receipt_and_original_report_code_as_safe_params():
    from backtest_data.fundamentals.public_version_batch import run_publication_batch
    calls, storage = [], []
    def fetch(request):
        calls.append(request)
        if request.url == LIST_URL:
            return public_list()
        return zip_archive([("report.xml", instance(entity="00123456", scheme="opendart.corp_code"))])
    result = run_publication_batch(PureSession(), targets=[target(source="opendart")], watermark=WATERMARK,
        fetch=fetch, now=lambda: CAPTURE, append=appender(storage))
    assert result["qualified_count"] == 1
    original = next(request for request in calls if request.url == DART_URL)
    assert original.params == {"rcept_no": RECEIPT, "reprt_code": "11011"}
    assert all("crtfc_key" not in request.params for request in calls)


def test_dart_json_without_complete_archived_original_mapping_is_recorded_unavailable():
    from backtest_data.fundamentals.public_version_batch import run_publication_batch
    item, storage = target(source="opendart"), []
    item.document.parser_version = "opendart_financial_json_v3"
    item.document.inventory["source_request_metadata"] = {"rcept_no": RECEIPT, "bsns_year": "2023", "fs_div": "CFS"}
    result = run_publication_batch(PureSession(), targets=[item], watermark=WATERMARK,
        fetch=lambda *_args: pytest.fail("latest JSON is not historical original"),
        now=lambda: CAPTURE, append=appender(storage))
    assert result["results"][0]["reason"] == "dart_json_original_mapping_unavailable"


def test_append_sql_failure_is_reported_and_other_whole_documents_continue():
    from backtest_data.fundamentals.public_version_batch import run_publication_batch
    storage = []
    def append(_session, **kwargs):
        if kwargs["document"].id == "A":
            raise RuntimeError("secret SQL params")
        return appender(storage)(_session, **kwargs)
    result = run_publication_batch(PureSession(), targets=[target(identifier="A", metadata=False), target(identifier="B", metadata=False)],
        watermark=WATERMARK, fetch=lambda *_args: pytest.fail("no plan"), now=lambda: CAPTURE, append=append)
    assert result["sql_failed_count"] == 1 and result["unavailable_count"] == 1
    assert result["next_cursor"] is None and result["resume_blocked_document_ids"] == ["A"]
    assert "secret" not in repr(result)


def test_batch_replay_uses_deterministic_attempt_evidence_while_preserving_capture_time():
    from backtest_data.fundamentals.public_version_batch import run_publication_batch
    from backtest_data.fundamentals.domain import stable_hash
    keys, clocks = set(), []
    def append(_session, **kwargs):
        keys.add(stable_hash((kwargs["status"], kwargs["evidence"])))
        clocks.append(kwargs["captured_at"])
        return SimpleNamespace(id="E")
    for _ in range(2):
        run_publication_batch(PureSession(), targets=[target(metadata=False)], watermark=WATERMARK,
            fetch=lambda *_args: pytest.fail("no plan"), now=lambda: CAPTURE, append=append)
    assert len(keys) == 1 and clocks == [CAPTURE, CAPTURE]


def test_unordered_or_duplicate_cursor_targets_are_rejected_before_any_append():
    from backtest_data.fundamentals.public_version_batch import run_publication_batch
    with pytest.raises(ValueError, match="ordered unique"):
        run_publication_batch(PureSession(), targets=[target(identifier="B"), target(identifier="A")], watermark=WATERMARK,
            fetch=lambda *_args: pytest.fail("unordered"), now=lambda: CAPTURE, append=lambda *_args, **_kwargs: pytest.fail("unordered"))


def test_existing_valid_qualification_skips_transport_and_duplicate_append():
    from backtest_data.fundamentals.public_version_batch import run_publication_batch
    from test_financial_public_availability import certificate
    from dataclasses import replace
    item = target()
    item = replace(item, existing_evidence=(certificate(item.document, item.filing),))
    result = run_publication_batch(PureSession(), targets=[item], watermark=WATERMARK,
        fetch=lambda *_args: pytest.fail("already verified"), now=lambda: CAPTURE,
        append=lambda *_args, **_kwargs: pytest.fail("duplicate certificate"))
    assert result["already_qualified_count"] == 1 and result["next_cursor"] == "D"


def test_issuer_object_must_match_the_sql_filing_identity():
    from backtest_data.fundamentals.public_version_batch import run_publication_batch
    item, storage = target(), []
    item.issuer.id = "OTHER"
    result = run_publication_batch(PureSession(), targets=[item], watermark=WATERMARK,
        fetch=lambda *_args: pytest.fail("cross-company target"), now=lambda: CAPTURE, append=appender(storage))
    assert result["results"][0]["reason"] == "issuer_binding_mismatch"


def test_malformed_job_metadata_is_an_explicit_gap_and_later_documents_continue():
    from backtest_data.fundamentals.public_version_batch import run_publication_batch
    item, storage = target(identifier="A"), []
    item.job_metadata[0]["filing"]["filingDate"] = {"malformed": True}
    result = run_publication_batch(PureSession(), targets=[item, target(identifier="B", metadata=False)],
        watermark=WATERMARK, fetch=lambda *_args: pytest.fail("invalid metadata"), now=lambda: CAPTURE, append=appender(storage))
    assert result["unavailable_count"] == 2 and result["next_cursor"] == "B"
    assert result["results"][0]["reason"] == "publication_metadata_invalid"


def test_sql_plan_has_fixed_capture_watermark_document_cursor_and_no_completeness_filter():
    from backtest_data.fundamentals.public_version_batch import publication_target_query
    from sqlalchemy.dialects import postgresql
    query = publication_target_query(watermark=WATERMARK, after_document_id="cursor", limit=20)
    compiled = query.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    where = sql.split("WHERE", 1)[1]
    assert "financial_source_document.captured_at <=" in where
    assert "financial_source_document.id >" in where
    assert "ORDER BY financial_source_document.id" in where
    assert "extraction_complete" not in where
    assert WATERMARK in compiled.params.values() and "cursor" in compiled.params.values() and 20 in compiled.params.values()


class ReadSession:
    def __init__(self, item, *, physical_count=None):
        self.item, self.physical_count, self.reads = item, physical_count, []
    def execute(self, statement):
        self.reads.append(statement)
        return [(self.item.document, self.item.filing, self.item.issuer)]
    def scalar(self, statement):
        self.reads.append(statement)
        return len(self.item.sql_facts) if self.physical_count is None else self.physical_count
    def scalars(self, statement):
        from backtest_data.fundamentals.models import SourceFinancialFact
        from backtest_data.ingestion.models import CollectionJob
        self.reads.append(statement)
        entity = statement.column_descriptions[0]["entity"]
        if entity is SourceFinancialFact:
            values = self.item.sql_facts
        elif entity is CollectionJob:
            values = [SimpleNamespace(request=request) for request in self.item.job_metadata]
        else:
            values = self.item.existing_evidence
        return SimpleNamespace(all=lambda: values)


def test_loader_reads_full_fact_document_and_reports_late_extraction_without_filtering_rows():
    from backtest_data.fundamentals.public_version_batch import load_publication_batch
    from backtest_data.fundamentals.models import SourceFinancialFact
    item = target()
    item.sql_facts[-1].extracted_at = CAPTURE
    session = ReadSession(item)
    loaded = load_publication_batch(session, watermark=WATERMARK)
    assert len(loaded) == 1 and len(loaded[0].sql_facts) == 2
    assert loaded[0].preflight_reason == "watermark_incomplete_document"
    fact_query = next(query for query in session.reads if query.column_descriptions[0].get("entity") is SourceFinancialFact
                      and query.column_descriptions[0].get("name") != "count")
    assert fact_query._limit_clause is None
    assert "captured_at <=" not in str(fact_query) and "extracted_at <=" not in str(fact_query)


def test_loader_counts_oversized_sql_inventory_and_never_loads_a_truncated_fact_subset():
    from backtest_data.fundamentals.public_version_batch import load_publication_batch
    from backtest_data.fundamentals.models import SourceFinancialFact
    session = ReadSession(target(), physical_count=200001)
    loaded = load_publication_batch(session, watermark=WATERMARK)
    assert loaded[0].sql_facts == () and loaded[0].preflight_reason == "document_inventory_limit"
    assert not any(query.column_descriptions[0].get("name") == "SourceFinancialFact" for query in session.reads)


def test_frozen_audit_document_ids_filter_in_sql_before_cursor_and_pagination():
    from backtest_data.fundamentals.public_version_batch import publication_target_query
    from sqlalchemy.dialects import postgresql
    query = publication_target_query(watermark=WATERMARK, after_document_id="A", limit=20,
        document_ids=["B", "D"])
    compiled = query.compile(dialect=postgresql.dialect())
    where = str(compiled).split("WHERE", 1)[1]
    assert "financial_source_document.id IN" in where
    assert where.index("financial_source_document.id IN") < where.index("ORDER BY") < where.index("LIMIT")
    assert ["B", "D"] in compiled.params.values()
    assert "A" in compiled.params.values()


@pytest.mark.parametrize("identities", [[], [""], [" "], ["A", "A"], ["A", None], "A", {"A": True}])
def test_frozen_audit_document_ids_are_nonempty_valid_unique_id_collections(identities):
    from backtest_data.fundamentals.public_version_batch import publication_target_query
    with pytest.raises(ValueError, match="frozen document IDs"):
        publication_target_query(watermark=WATERMARK, document_ids=identities)


def test_loader_passes_frozen_audit_ids_to_the_sql_filter_instead_of_trimming_rows():
    from backtest_data.fundamentals.public_version_batch import load_publication_batch
    session = ReadSession(target())
    loaded = load_publication_batch(session, watermark=WATERMARK, document_ids=("D", "other"))
    assert len(loaded) == 1 and len(loaded[0].sql_facts) == 2
    query = session.reads[0]
    assert "financial_source_document.id IN" in str(query).split("WHERE", 1)[1]
    assert ["D", "other"] in query.compile().params.values()


def test_absent_frozen_id_filter_preserves_existing_query_contract():
    from backtest_data.fundamentals.public_version_batch import publication_target_query
    query = publication_target_query(watermark=WATERMARK)
    assert "financial_source_document.id IN" not in str(query).split("WHERE", 1)[1]
