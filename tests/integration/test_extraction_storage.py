from datetime import datetime, timedelta, timezone
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from backtest_data.catalog.models import Issuer
from backtest_data.fundamentals.xbrl import parse_xbrl
from backtest_data.fundamentals.storage import store_extraction
from backtest_data.fundamentals.models import FinancialSourceDocument, SourceFinancialFact
from backtest_data.releases.models import SelectionDecision


pytestmark = pytest.mark.integration


def example_xml(amount):
    return f'''<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:c="urn:custom" xmlns:i="http://www.xbrl.org/2003/iso4217">
    <context id="ctx"><entity><identifier scheme="test">1</identifier></entity><period><instant>2020-12-31</instant></period></context>
    <unit id="USD"><measure>i:USD</measure></unit><c:UsefulNote contextRef="ctx" unitRef="USD" decimals="0">{amount}</c:UsefulNote></xbrl>'''.encode()


def test_sql_repeat_preserves_clocks_and_same_accession_replacement_is_not_backdated(db_session):
    issuer = Issuer(market="US", source_identifier="storage-example", name="Example")
    db_session.add(issuer)
    db_session.flush()
    available = datetime(2021, 3, 1, tzinfo=timezone.utc)
    captured = datetime(2021, 5, 1, tzinfo=timezone.utc)
    common = dict(issuer_id=issuer.id, source="sec", accession="storage-example", form="10-K",
                  source_url="https://www.sec.gov/test.xml", available_at=available, captured_at=captured)
    xml = example_xml(100)
    first = store_extraction(db_session, **common, content=xml, result=parse_xbrl(xml))
    repeat = store_extraction(db_session, **common, content=xml, result=parse_xbrl(xml))
    assert repeat.id == first.id
    changed = example_xml(110)
    replacement = store_extraction(db_session, **common, content=changed, result=parse_xbrl(changed))
    assert replacement.available_at == captured
    assert first.available_at == available
    facts = db_session.scalars(select(SourceFinancialFact).where(SourceFinancialFact.issuer_id == issuer.id)).all()
    assert {item.value for item in facts} == {Decimal(100), Decimal(110)}
    assert all(item.first_seen_at >= captured for item in facts)
    decisions = db_session.scalars(select(SelectionDecision).where(SelectionDecision.record_domain == "source_financial_fact")).all()
    assert len(decisions) == 2
    assert {decision.record_id for decision in decisions} == {item.id for item in facts}
    assert all(decision.evidence_grade == "structural_only" for decision in decisions)
    assert all(decision.recorded_at >= captured for decision in decisions)


def test_alias_url_repeats_use_sql_identity_and_wrong_cik_is_quarantined(db_session):
    from backtest_data.fundamentals.models import FinancialSourceDocument
    issuer = Issuer(market="US", source_identifier="CIK0000000001", name="Example")
    db_session.add(issuer)
    db_session.flush()
    now = datetime.now(timezone.utc)
    common = dict(issuer_id=issuer.id, source="sec", accession="aliases", form="10-K", available_at=now, captured_at=now)
    xml = example_xml(100)
    first = store_extraction(db_session, **common, source_url="https://www.sec.gov/alias-one.xml", content=xml, result=parse_xbrl(xml))
    repeated = store_extraction(db_session, **common, source_url="https://www.sec.gov/alias-two.xml", content=xml, result=parse_xbrl(xml))
    assert repeated.id == first.id
    wrong = xml.replace(b'scheme="test">1', b'scheme="http://www.sec.gov/CIK">2')
    quarantined = store_extraction(db_session, **{**common, "accession": "wrong-issuer"}, source_url="https://www.sec.gov/wrong.xml", content=wrong, result=parse_xbrl(wrong))
    assert not quarantined.extraction_complete
    assert quarantined.inventory["issuer_identity"]["status"] == "mismatch"
    fact_ids = select(SourceFinancialFact.id).where(SourceFinancialFact.document_id == quarantined.id)
    assert not db_session.scalars(select(SelectionDecision).where(SelectionDecision.record_domain == "source_financial_fact", SelectionDecision.record_id.in_(fact_ids))).all()


@pytest.mark.parametrize("context_reference", [b"", b' contextRef="absent"'])
def test_sql_retains_numeric_nodes_with_missing_context_in_quarantine(db_session, context_reference):
    """Exercise the unit-tag numeric node from the parser's missing-context regression."""
    from backtest_data.releases.build import financial_inventory
    issuer = Issuer(market="US", source_identifier="missing-context-example", name="Example")
    db_session.add(issuer)
    db_session.flush()
    xml = example_xml(100).replace(b"</xbrl>",
        b'<c:Malformed id="unresolved-node" unitRef="USD"' + context_reference + b'>123</c:Malformed></xbrl>')
    parsed = parse_xbrl(xml)
    assert not parsed.complete and parsed.facts[-1].parse_status == "missing_context"
    now = datetime.now(timezone.utc)
    common = dict(issuer_id=issuer.id, source="sec", accession="missing-context-example", form="10-K",
                  source_url="https://www.sec.gov/missing-context.xml", available_at=now, captured_at=now,
                  content=xml, result=parsed)
    document = store_extraction(db_session, **common)
    repeated = store_extraction(db_session, **common)
    assert repeated.id == document.id and not document.extraction_complete
    rows = db_session.scalars(select(SourceFinancialFact).where(SourceFinancialFact.document_id == document.id)).all()
    assert len(rows) == document.inventory["counts"]["numeric_nodes"] == document.inventory["counts"]["extracted_nodes"] == 2
    assert {node for row in rows for node in row.evidence["source_node_ids"]} == {"node:1", "unresolved-node"}
    assert document.inventory["counts"]["context_ids"] == ["ctx"]
    missing = next(row for row in rows if row.evidence["parse_status"] == "missing_context")
    assert missing.raw_value == "123" and missing.value is None and "Malformed" in missing.evidence["raw_numeric_node"]
    placeholder = document.inventory["contexts"][missing.context_id]
    assert placeholder["reported"] is False and placeholder["resolution_status"] == "missing_context"
    assert placeholder["entity_identifier"] is placeholder["entity_scheme"] is None
    assert placeholder["period_kind"] == placeholder["scope"] == "unknown"
    assert placeholder["period_start"] is placeholder["period_end"] is placeholder["raw_xml"] is None
    assert placeholder["source_node_ids"] == ["unresolved-node"]
    assert financial_inventory(document, rows) == ["financial_extraction_incomplete"]
    decisions = db_session.scalars(select(SelectionDecision)).all()
    assert len(decisions) == 1
    assert decisions[0].record_domain == "financial_source_document" and decisions[0].status == "quarantined"


@pytest.mark.parametrize("incomplete", [False, True])
def test_unreported_context_requires_the_specific_missing_context_issue(db_session, incomplete):
    from backtest_data.fundamentals.domain import ExtractionIssue
    issuer = Issuer(market="US", source_identifier="undocumented-context-example", name="Example")
    db_session.add(issuer)
    db_session.flush()
    xml = example_xml(100).replace(b"</xbrl>", b'<c:Malformed id="unresolved-node" unitRef="USD">123</c:Malformed></xbrl>')
    parsed = parse_xbrl(xml)
    # An unrelated failure cannot authorize an invented context, even in quarantine.
    issues = (ExtractionIssue("invalid_numeric", "unresolved-node", "Unrelated failure"),) if incomplete else ()
    undocumented = replace(parsed, issues=issues)
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="SQL extraction inventory mismatch"):
        store_extraction(db_session, issuer_id=issuer.id, source="sec", accession="undocumented-context-example",
            form="10-K", source_url="https://www.sec.gov/undocumented-context.xml", available_at=now, captured_at=now,
            content=xml, result=undocumented)
    assert not db_session.scalars(select(FinancialSourceDocument)).all()
    assert not db_session.scalars(select(SourceFinancialFact)).all()
    assert not db_session.scalars(select(SelectionDecision)).all()


def test_incomplete_document_must_still_conserve_its_full_numeric_node_inventory(db_session):
    from backtest_data.fundamentals.domain import ExtractionIssue
    issuer = Issuer(market="US", source_identifier="incomplete-inventory-example", name="Example")
    db_session.add(issuer)
    db_session.flush()
    xml = example_xml(100)
    parsed = parse_xbrl(xml)
    partial = replace(parsed, inventory=replace(parsed.inventory, numeric_nodes=2),
        issues=(ExtractionIssue("unsupported_format", "lost-node", "A declared numeric node was not emitted"),))
    assert not partial.complete
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="SQL extraction inventory mismatch"):
        store_extraction(db_session, issuer_id=issuer.id, source="sec", accession="incomplete-inventory-example",
            form="10-K", source_url="https://www.sec.gov/incomplete-inventory.xml", available_at=now, captured_at=now,
            content=xml, result=partial)
    assert not db_session.scalars(select(FinancialSourceDocument)).all()
    assert not db_session.scalars(select(SourceFinancialFact)).all()
    assert not db_session.scalars(select(SelectionDecision)).all()


def many_facts_xml(count=101):
    nodes = b"".join(f'<c:Amount{offset} id="node-{offset}" contextRef="ctx" unitRef="USD" decimals="4">{offset}.0100</c:Amount{offset}>'.encode()
        for offset in range(count))
    return example_xml(100).replace(b'<c:UsefulNote contextRef="ctx" unitRef="USD" decimals="0">100</c:UsefulNote>', nodes)


def financial_lease(session):
    from backtest_data.db import utcnow
    from backtest_data.ingestion.jobs import acquire_job, plan_job
    issuer = Issuer(market="US", source_identifier="CIK0000000001", name="Fenced extraction")
    session.add(issuer)
    session.flush()
    now = utcnow()
    job = plan_job(session, source="sec", domain="filing_sql", entity_id=issuer.id, request={"market": "US"})
    lease = acquire_job(session, "financial-test", now, timedelta(minutes=5), domains=["filing_sql"])
    assert lease.job_id == job.id
    return issuer, job, lease, now


@pytest.mark.parametrize("slow_table", ["source_financial_fact", "selection_decision"])
def test_real_postgres_document_write_renews_valid_lease_between_batches(db_session, slow_table):
    from backtest_data.ingestion.jobs import heartbeat_job
    from backtest_data.fundamentals.storage import FINANCIAL_WRITE_BATCH_SIZE
    issuer, job, lease, now = financial_lease(db_session)
    clock = [now]
    write_sizes = []

    def advance(connection, cursor, statement, parameters, context, executemany):
        if context.compiled and context.isinsert and context.compiled.statement.table.name == slow_table:
            count = sum(key.startswith("id_m") for key in context.compiled.params)
            assert count > 0
            write_sizes.append(count)
            clock[0] += timedelta(seconds=count * 4)

    connection = db_session.connection()
    event.listen(connection, "after_cursor_execute", advance)
    xml = many_facts_xml()
    common = dict(issuer_id=issuer.id, source="sec", accession="fenced-batches", form="10-K",
        source_url="https://www.sec.gov/fenced.xml", available_at=now, captured_at=now,
        content=xml, result=parse_xbrl(xml),
        fence=lambda session: heartbeat_job(session, lease, clock[0], timedelta(minutes=5)))
    try:
        document = store_extraction(db_session, **common)
        original = {row.id: row.first_seen_at for row in db_session.scalars(
            select(SourceFinancialFact).where(SourceFinancialFact.document_id == document.id))}
        repeated = store_extraction(db_session, **common)
    finally:
        event.remove(connection, "after_cursor_execute", advance)
    assert repeated.id == document.id and clock[0] - now > timedelta(minutes=5)
    assert max(write_sizes) <= FINANCIAL_WRITE_BATCH_SIZE and sum(write_sizes) == 101
    rows = db_session.scalars(select(SourceFinancialFact).where(SourceFinancialFact.document_id == document.id)).all()
    decisions = db_session.scalars(select(SelectionDecision).where(SelectionDecision.record_id.in_(original))).all()
    assert len(rows) == len(decisions) == 101
    assert {row.id: row.first_seen_at for row in rows} == original
    assert {row.value for row in rows} == {Decimal(f"{offset}.0100") for offset in range(101)}
    assert all(row.available_at == row.captured_at == now for row in rows)
    db_session.refresh(job)
    assert job.lease_until > clock[0]


def test_real_postgres_expired_owner_does_not_autoflush_pending_evidence(db_session):
    from backtest_data.ingestion.jobs import LeaseLost, heartbeat_job
    issuer, job, lease, now = financial_lease(db_session)
    pending = Issuer(market="US", source_identifier="fenced-pending", name="Pending evidence")
    db_session.add(pending)
    flushes = []

    def flushed(session, context, instances):
        flushes.append(True)

    event.listen(db_session, "before_flush", flushed)
    xml = example_xml(100)
    try:
        with pytest.raises(LeaseLost, match="Expired or superseded lease"):
            store_extraction(db_session, issuer_id=issuer.id, source="sec", accession="expired-fence", form="10-K",
                source_url="https://www.sec.gov/expired.xml", available_at=now, captured_at=now,
                content=xml, result=parse_xbrl(xml),
                fence=lambda session: heartbeat_job(session, lease, now + timedelta(seconds=301), timedelta(minutes=5)))
        assert not flushes and pending in db_session.new and pending.id is None
    finally:
        event.remove(db_session, "before_flush", flushed)


def test_real_postgres_expiry_after_fact_insert_rolls_back_only_extraction(db_session):
    from backtest_data.catalog.models import CatalogObservation
    from backtest_data.fundamentals.models import Filing
    from backtest_data.ingestion.jobs import LeaseLost, heartbeat_job
    issuer, job, lease, now = financial_lease(db_session)
    clock = [now]

    def expire(connection, cursor, statement, parameters, context, executemany):
        if context.compiled and context.isinsert and context.compiled.statement.table.name == "source_financial_fact":
            clock[0] += timedelta(seconds=301)

    connection = db_session.connection()
    event.listen(connection, "after_cursor_execute", expire)
    xml = many_facts_xml()
    try:
        with pytest.raises(LeaseLost, match="Expired or superseded lease"):
            store_extraction(db_session, issuer_id=issuer.id, source="sec", accession="expiry-rollback", form="10-K",
                source_url="https://www.sec.gov/expiry.xml", available_at=now, captured_at=now,
                content=xml, result=parse_xbrl(xml),
                fence=lambda session: heartbeat_job(session, lease, clock[0], timedelta(minutes=5)))
    finally:
        event.remove(connection, "after_cursor_execute", expire)
    assert db_session.get(Issuer, issuer.id) is issuer
    assert not db_session.scalars(select(Filing).where(Filing.issuer_id == issuer.id)).all()
    assert not db_session.scalars(select(FinancialSourceDocument)).all()
    assert not db_session.scalars(select(SourceFinancialFact)).all()
    assert not db_session.scalars(select(SelectionDecision)).all()
    assert not db_session.scalars(select(CatalogObservation).where(CatalogObservation.source == "financial_provenance")).all()
    db_session.refresh(job)
    assert job.status == "running" and job.lease_until == now + timedelta(minutes=5)


def test_inventory_rejects_duplicate_node_in_later_streaming_batch(db_session):
    issuer = Issuer(market="US", source_identifier="late-inventory", name="Late inventory")
    db_session.add(issuer)
    db_session.flush()
    xml = many_facts_xml()
    parsed = parse_xbrl(xml)
    corrupted = replace(parsed, facts=(*parsed.facts[:-1], replace(parsed.facts[-1], source_node_ids=("node-0",))))
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="SQL extraction inventory mismatch"):
        store_extraction(db_session, issuer_id=issuer.id, source="sec", accession="late-inventory", form="10-K",
            source_url="https://www.sec.gov/late.xml", available_at=now, captured_at=now, content=xml, result=corrupted)
    assert not db_session.scalars(select(FinancialSourceDocument)).all()
    assert not db_session.scalars(select(SourceFinancialFact)).all()
    assert not db_session.scalars(select(SelectionDecision)).all()


def test_repeat_inventory_gate_rejects_parser_node_mismatch_without_changing_original(db_session):
    from backtest_data.catalog.models import CatalogObservation
    issuer = Issuer(market="US", source_identifier="repeat-inventory", name="Repeat inventory")
    db_session.add(issuer)
    db_session.flush()
    xml = many_facts_xml()
    parsed = parse_xbrl(xml)
    now = datetime.now(timezone.utc)
    common = dict(issuer_id=issuer.id, source="sec", accession="repeat-inventory", form="10-K",
        source_url="https://www.sec.gov/repeat.xml", available_at=now, captured_at=now, content=xml)
    inventory_reads = []

    def inventory_read(connection, cursor, statement, parameters, context, executemany):
        if (context.compiled and context.compiled.statement.is_select
                and "raw_value" in context.compiled.statement.selected_columns):
            inventory_reads.append(True)

    connection = db_session.connection()
    event.listen(connection, "after_cursor_execute", inventory_read)
    try:
        original = store_extraction(db_session, **common, result=parsed)
        assert len(inventory_reads) == 1
        assert store_extraction(db_session, **common, result=parsed).id == original.id
        assert len(inventory_reads) == 2
    finally:
        event.remove(connection, "after_cursor_execute", inventory_read)
    first_seen = {row.id: row.first_seen_at for row in db_session.scalars(select(SourceFinancialFact))}
    corrupted = replace(parsed, facts=(*parsed.facts[:-1], replace(parsed.facts[-1], source_node_ids=("node-0",))))
    with pytest.raises(ValueError, match="SQL extraction inventory mismatch"):
        store_extraction(db_session, **{**common, "source_url": "https://www.sec.gov/failed-alias.xml"}, result=corrupted)
    assert db_session.scalars(select(FinancialSourceDocument.id)).all() == [original.id]
    assert {row.id: row.first_seen_at for row in db_session.scalars(select(SourceFinancialFact))} == first_seen
    assert len(db_session.scalars(select(SelectionDecision)).all()) == 101
    observations = db_session.scalars(select(CatalogObservation).where(CatalogObservation.source == "financial_provenance")).all()
    assert len(observations) == 1 and observations[0].fields["source_url"] == common["source_url"]
