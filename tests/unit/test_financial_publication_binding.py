"""Full numeric inventories, never selected metrics, bind public originals."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import localcontext
from types import SimpleNamespace

import pytest

from backtest_data.fundamentals.storage import source_fact_record
from backtest_data.fundamentals.xbrl import parse_xbrl


NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
XML = b'''<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:c="urn:custom" xmlns:i="http://www.xbrl.org/2003/iso4217">
<context id="ctx"><entity><identifier scheme="test">1</identifier></entity><period><instant>2023-12-31</instant></period></context>
<unit id="USD"><measure>i:USD</measure></unit><c:Amount contextRef="ctx" unitRef="USD" decimals="0">100</c:Amount></xbrl>'''


def sql_inventory(content=XML):
    result = parse_xbrl(content)
    document = SimpleNamespace(id="D", extraction_complete=result.complete, inventory={
        "counts": asdict(result.inventory), "contexts": {key: asdict(value) for key, value in result.contexts.items()},
        "units": result.units, "issues": [], "source_format_complete": result.complete})
    rows = [SimpleNamespace(**source_fact_record(fact, document_id="D", issuer_id="I",
        available_at=NOW, captured_at=NOW, extracted_at=NOW)) for fact in result.facts]
    return document, rows


def test_numeric_binding_matches_opaque_context_and_namespace_aliases_structurally():
    from backtest_data.fundamentals.public_availability import numeric_binding
    document, rows = sql_inventory()
    aliased = XML.replace(b'id="ctx"', b'id="another"').replace(b'contextRef="ctx"', b'contextRef="another"')
    aliased = aliased.replace(b'xmlns:c="urn:custom"', b'xmlns:other="urn:custom"').replace(b'c:Amount', b'other:Amount')
    other_document, other_rows = sql_inventory(aliased)
    first = numeric_binding(document, rows)
    other = numeric_binding(other_document, other_rows)
    assert first == other
    assert len(first["sql_digest"]) == 64
    assert first["numeric_nodes"] == first["extracted_nodes"] == first["fact_count"] == 1
    assert first["complete"] is True


@pytest.mark.parametrize("old,new", [(b'>100<', b'>101<'), (b'decimals="0"', b'decimals="2"'),
    (b'scheme="test"', b'scheme="other"'), (b'>1</identifier>', b'>2</identifier>'),
    (b'2023-12-31', b'2023-12-30'), (b'i:USD', b'i:KRW'), (b'urn:custom', b'urn:different')])
def test_numeric_binding_digest_covers_all_original_semantics(old, new):
    from backtest_data.fundamentals.public_availability import numeric_binding
    document, rows = sql_inventory()
    changed_document, changed_rows = sql_inventory(XML.replace(old, new))
    assert numeric_binding(document, rows)["sql_digest"] != numeric_binding(changed_document, changed_rows)["sql_digest"]


@pytest.mark.parametrize("damage", ["missing_row", "missing_nodes", "duplicate_nodes", "unsupported", "incomplete"])
def test_numeric_binding_rejects_incomplete_or_unsupported_sql_inventory(damage):
    from backtest_data.fundamentals.public_availability import numeric_binding
    document, rows = sql_inventory()
    if damage == "missing_row":
        rows = []
    elif damage == "missing_nodes":
        rows[0].evidence["source_node_ids"] = []
    elif damage == "duplicate_nodes":
        rows[0].evidence["source_node_ids"] = ["node:1", "node:1"]
    elif damage == "unsupported":
        rows[0].evidence["parse_status"] = "unsupported_format"
    else:
        document.extraction_complete = False
    with pytest.raises(ValueError, match="numeric_binding"):
        numeric_binding(document, rows)


def test_publication_model_is_separate_and_does_not_change_legacy_columns():
    from backtest_data.db import Base, load_models
    from backtest_data.fundamentals.models import FinancialSourceDocument
    from backtest_data.fundamentals.publication_models import FinancialPublicationEvidence
    original = {column.name for column in FinancialSourceDocument.__table__.columns}
    load_models()
    assert "financial_publication_evidence" in Base.metadata.tables
    table = FinancialPublicationEvidence.__table__
    assert {"document_id", "filing_id", "issuer_id", "source", "accession", "document_hash", "status",
        "public_day", "effective_available_at", "precision", "timezone", "policy_version", "captured_at",
        "recorded_at", "assertion_hash", "supersedes_assertion_hash", "evidence"} <= {column.name for column in table.columns}
    assert {column.name for column in FinancialSourceDocument.__table__.columns} == original


def test_binding_never_rounds_long_numbers_under_the_decimal_context():
    from backtest_data.fundamentals.public_availability import numeric_binding
    first_document, first_rows = sql_inventory(XML.replace(b">100<", b">123456789012345678901234567890123456789<"))
    second_document, second_rows = sql_inventory(XML.replace(b">100<", b">123456789012345678901234567890123456788<"))
    with localcontext() as context:
        context.prec = 8
        low_precision = numeric_binding(first_document, first_rows)
    with localcontext() as context:
        context.prec = 50
        high_precision = numeric_binding(first_document, first_rows)
    assert low_precision == high_precision
    assert low_precision["sql_digest"] != numeric_binding(second_document, second_rows)["sql_digest"]


def test_non_dimensional_entity_content_is_unsupported_even_when_parser_retains_numeric_nodes():
    from backtest_data.fundamentals.public_availability import numeric_binding
    content = XML.replace(b"</entity>", b'<c:OpaqueConstraint>unmodeled</c:OpaqueConstraint></entity>')
    document, rows = sql_inventory(content)
    assert document.extraction_complete
    with pytest.raises(ValueError, match="numeric_binding: unsupported"):
        numeric_binding(document, rows)


def test_structurally_equivalent_opaque_contexts_with_conflicting_values_are_ambiguous():
    from backtest_data.fundamentals.public_availability import numeric_binding
    context = XML.split(b'<context id="ctx">')[1].split(b"</context>")[0]
    extra = b'<context id="alias">' + context + b'</context><c:Amount contextRef="alias" unitRef="USD" decimals="0">101</c:Amount>'
    document, rows = sql_inventory(XML.replace(b"</xbrl>", extra + b"</xbrl>"))
    assert document.extraction_complete
    with pytest.raises(ValueError, match="numeric_binding: ambiguous"):
        numeric_binding(document, rows)


@pytest.mark.parametrize("field,value", [("unit", "EUR"), ("original_unit", "i:EUR"), ("scope", "consolidated")])
def test_stored_sql_unit_and_scope_must_agree_with_their_original_expanded_structure(field, value):
    from backtest_data.fundamentals.public_availability import numeric_binding
    document, rows = sql_inventory()
    setattr(rows[0], field, value)
    with pytest.raises(ValueError, match="numeric_binding"):
        numeric_binding(document, rows)


def test_certificate_recording_cannot_predate_the_complete_bound_sql_extraction():
    from backtest_data.fundamentals.public_availability import _validate_recording_clocks
    document, rows = sql_inventory()
    document.captured_at = NOW - timedelta(days=2)
    filing = SimpleNamespace(captured_at=NOW - timedelta(days=3))
    captured = NOW - timedelta(days=1)
    with pytest.raises(ValueError, match="publication_recording_before_bound_observation"):
        _validate_recording_clocks(document, filing, rows, captured_at=captured, recorded_at=NOW - timedelta(seconds=1))
    _validate_recording_clocks(document, filing, rows, captured_at=captured, recorded_at=NOW)
