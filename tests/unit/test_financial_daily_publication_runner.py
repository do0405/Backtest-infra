from datetime import datetime, timezone
import importlib
from types import SimpleNamespace as N
import pytest
from sqlalchemy.dialects import postgresql


WATERMARK = datetime(2026, 10, 10, tzinfo=timezone.utc)


def api():
    try:
        return importlib.import_module("backtest_data.fundamentals.daily_publication_runner")
    except ModuleNotFoundError:
        pytest.fail("Daily publication backfill runner is missing")


def target():
    return N(id="full-manifest", content_hash="a" * 64,
             members=[{"market": "KR", "security_id": f"k{i}"} for i in range(4507)] +
                     [{"market": "US", "security_id": f"u{i}"} for i in range(13251)])


def test_daily_progress_preserves_full_target_and_frozen_source_watermark():
    progress = api().new_daily_progress(target(), watermark=WATERMARK, total_documents=25000)
    assert progress["original_target"]["security_count"] == 17758
    assert progress["original_target"]["market_counts"] == {"KR": 4507, "US": 13251}
    assert progress["total_documents"] == 25000
    assert progress["watermark"] == WATERMARK.isoformat()
    assert progress["coverage_complete"] is False
    assert progress["cursor"] is None


def test_resume_cannot_change_target_watermark_or_policy():
    progress = api().new_daily_progress(target(), watermark=WATERMARK, total_documents=25000)
    assert api().validate_daily_progress(progress, target()) is progress
    for field, value in [("watermark", "2026-10-11T00:00:00+00:00"), ("policy_version", "different")]:
        changed = progress | {field: value}
        with pytest.raises(ValueError):
            api().validate_daily_progress(changed, target())
    wrong = target()
    wrong.content_hash = "b" * 64
    with pytest.raises(ValueError):
        api().validate_daily_progress(progress, wrong)


def test_daily_document_query_is_bounded_ordered_and_not_sample_limited():
    query = api().daily_target_query(watermark=WATERMARK, after_document_id="cursor1", limit=25)
    compiled = query.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "financial_source_document.id >" in sql
    assert "ORDER BY financial_source_document.id" in sql
    assert "LIMIT" in sql
    assert "issuer.id IN" not in sql
    assert 25 in compiled.params.values()
    assert WATERMARK in compiled.params.values()
    with pytest.raises(ValueError):
        api().daily_target_query(watermark=WATERMARK, limit=0)


def test_matching_source_row_metadata_query_uses_exact_version_not_accounting_period():
    f = N(source="sec", accession="0000000001-24-000001")
    query = api().daily_source_metadata_query(document_id="document1", filing=f, watermark=WATERMARK)
    compiled = query.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "DISTINCT" in sql
    assert "period_end" not in sql
    assert "document1" in compiled.params.values()
    assert f.accession in compiled.params.values()
    assert "filed" in compiled.params.values()


def test_filing_metadata_query_uses_existing_entity_index_and_bounded_rows():
    f = N(source="sec", issuer_id="issuer1", accession="0000000001-24-000001")
    query = api().daily_metadata_lookup_query(f, f.accession)
    compiled = query.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "collection_job.entity_id =" in sql
    assert "collection_job.domain =" in sql
    assert "LIMIT" in sql
    assert f.accession in compiled.params.values()
