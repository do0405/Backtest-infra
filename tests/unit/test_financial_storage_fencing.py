"""Deterministic PostgreSQL delays exercise the real financial collector/storage."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from backtest_data.catalog.models import Issuer
from backtest_data.db import new_id
from backtest_data.fundamentals import collection
from backtest_data.fundamentals.models import Filing, FinancialSourceDocument, SourceFinancialFact
from backtest_data.fundamentals.storage import store_extraction
from backtest_data.ingestion.jobs import JobLease, LeaseLost, heartbeat_job
from backtest_data.ingestion.models import CollectionJob
from backtest_data.releases.models import SelectionDecision


CAPTURED = datetime(2024, 7, 10, 18, tzinfo=timezone.utc)
TTL = timedelta(minutes=5)
LEASE = JobLease("financial-job", "collector", 7)


class DelayedRows:
    def __init__(self, session, rows, seconds_per_row=0):
        self.session, self.rows, self.seconds_per_row = session, rows, seconds_per_row

    def all(self):
        self.session.now += timedelta(seconds=len(self.rows) * self.seconds_per_row)
        return list(self.rows)

    def __iter__(self):
        return iter(self.all())

    def partitions(self, size):
        for offset in range(0, len(self.rows), size):
            batch = self.rows[offset:offset + size]
            self.session.now += timedelta(seconds=len(batch) * self.seconds_per_row)
            yield batch

    def close(self):
        self.session.closed_reads += 1


class DelayedFinancialSession:
    def __init__(self, *, slow_table=None, seconds_per_row=0, inventory_seconds_per_row=0,
                 prior_read_seconds=0, flush_seconds=0, expired=False, market="US", issuer_read_seconds=0):
        self.now = CAPTURED
        self.lease_until = self.now + (timedelta(seconds=-1) if expired else TTL)
        self.slow_table, self.seconds_per_row = slow_table, seconds_per_row
        self.inventory_seconds_per_row = inventory_seconds_per_row
        self.prior_read_seconds, self.flush_seconds = prior_read_seconds, flush_seconds
        self.market, self.issuer_read_seconds = market, issuer_read_seconds
        self.suppressed_autoflush = 0
        self.pending, self.filings, self.documents = [], [], []
        self.facts, self.decisions, self.observations = [], [], []
        self.flushed, self.write_sizes, self.heartbeats = [], [], []
        self.closed_reads = 0
        self.savepoints = 0
        self.jobs = []
        self.provider_fetches = 0
        self.inventory_reads = 0

    @property
    @contextmanager
    def no_autoflush(self):
        self.suppressed_autoflush += 1
        try:
            yield
        finally:
            self.suppressed_autoflush -= 1

    def fence(self, session):
        assert session is self
        heartbeat_job(self, LEASE, self.now, TTL)

    def _autoflush(self):
        if not self.suppressed_autoflush:
            self.flush()

    def add(self, item):
        self.pending.append(item)

    def flush(self):
        if not self.pending:
            return
        self.now += timedelta(seconds=self.flush_seconds)
        for item in self.pending:
            if item.id is None:
                item.id = new_id()
            self.flushed.append(item)
            if isinstance(item, Filing):
                self.filings.append(item)
            elif isinstance(item, FinancialSourceDocument):
                self.documents.append(item)
            elif isinstance(item, SelectionDecision):
                self.decisions.append({"id": item.id, "record_domain": item.record_domain,
                    "record_id": item.record_id, "status": item.status})
        self.pending.clear()

    @contextmanager
    def begin_nested(self):
        # SQLAlchemy always flushes before opening a SAVEPOINT, including with
        # no_autoflush enabled. Ownership must be checked before this call.
        self.flush()
        self.savepoints += 1
        snapshot = [list(items) for items in (self.filings, self.documents, self.facts,
            self.decisions, self.observations, self.flushed)]
        try:
            yield
            self.flush()
        except Exception:
            (self.filings, self.documents, self.facts, self.decisions,
                self.observations, self.flushed) = snapshot
            self.pending.clear()
            raise

    def get(self, model, identifier):
        assert model is Issuer
        self._autoflush()
        self.now += timedelta(seconds=self.issuer_read_seconds)
        self.issuer_read_seconds = 0
        return SimpleNamespace(id=identifier, source_identifier="DART:00000001" if self.market == "KR" else "CIK0000000001")

    def scalar(self, statement):
        self._autoflush()
        entity = statement.column_descriptions[0]["entity"]
        if entity is Filing:
            return self.filings[0] if self.filings else None
        if entity is CollectionJob:
            params = statement.compile(dialect=postgresql.dialect()).params
            if "identity_hash_1" in params:
                return next((job for job in self.jobs if job.identity_hash == params["identity_hash_1"]), None)
            return next((job for job in self.jobs if job.entity_id == params["entity_id_1"]), None)
        assert entity is SelectionDecision
        return next((row["id"] for row in self.decisions
            if row["record_domain"] == "financial_source_document"), None)

    def scalars(self, statement):
        self._autoflush()
        description = statement.column_descriptions[0]
        entity = description["entity"]
        if entity is FinancialSourceDocument:
            self.now += timedelta(seconds=self.prior_read_seconds)
            self.prior_read_seconds = 0
            return DelayedRows(self, self.documents)
        if entity is SourceFinancialFact:
            if description["name"] == "id":
                return DelayedRows(self, [row.id for row in self.facts], self.inventory_seconds_per_row)
            self.inventory_reads += 1
            return DelayedRows(self, self.facts, self.inventory_seconds_per_row)
        assert entity is SelectionDecision
        return DelayedRows(self, [row["record_id"] for row in self.decisions
            if row["record_domain"] == "source_financial_fact"])

    def execute(self, statement):
        self._autoflush()
        if statement.is_select:
            return SimpleNamespace()
        compiled = statement.compile(dialect=postgresql.dialect())
        params = compiled.params
        if statement.is_update:
            assert statement.table.name == "collection_job"
            valid = self.lease_until > self.now
            if valid:
                self.lease_until = params["lease_until"]
                self.heartbeats.append(self.now)
            return SimpleNamespace(rowcount=int(valid))
        assert statement.is_insert
        records = {}
        for name, value in params.items():
            field, _, suffix = name.rpartition("_m")
            if suffix.isdigit():
                records.setdefault(int(suffix), {})[field] = value
        rows = list(records.values()) or [params]
        table = statement.table.name
        self.write_sizes.append((table, len(rows)))
        if table == self.slow_table:
            self.now += timedelta(seconds=len(rows) * self.seconds_per_row)
        if table == "source_financial_fact":
            existing = {row.signature for row in self.facts}
            self.facts.extend(SimpleNamespace(**row) for row in rows if row["signature"] not in existing)
        elif table == "selection_decision":
            self.decisions.extend(rows)
        elif table == "collection_job":
            self.jobs.extend(SimpleNamespace(**row) for row in rows)
        else:
            assert table == "catalog_observation"
            self.observations.extend(rows)
        return SimpleNamespace()


def collect_document(session, monkeypatch, *, count=101):
    @contextmanager
    def tx(engine):
        yield session
    monkeypatch.setattr(collection, "transaction", tx)
    nodes = "".join(f'<t:Amount{offset} id="node-{offset}" contextRef="c" unitRef="u" decimals="4">{offset}.0100</t:Amount{offset}>'
        for offset in range(count))
    xml = (f'<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:t="urn:test">'
        '<context id="c"><entity><identifier scheme="http://www.sec.gov/CIK">1</identifier></entity>'
        '<period><instant>2020-12-31</instant></period></context><unit id="u"><measure>t:USD</measure></unit>'
        f'{nodes}</xbrl>').encode()
    base = "https://www.sec.gov/Archives/edgar/data/1/000000000124000001/"
    client = SimpleNamespace(fetch=lambda url: SimpleNamespace(content=(
        b'{"directory":{"item":[{"name":"example_htm.xml"}]}}' if url == base + "index.json" else xml),
        captured_at=CAPTURED))
    result = collection.collect_filing(None, {"issuer_id": "issuer", "cik": "1",
        "filing": {"accessionNumber": "0000000001-24-000001", "form": "10-K"},
        "disclosure_profile": "periodic_report"}, source="sec", dart=None, sec=client, fence=session.fence)
    return result, xml


@pytest.mark.parametrize("slow_table", ["source_financial_fact", "selection_decision"])
def test_document_keeps_valid_lease_during_slow_fact_and_decision_writes(monkeypatch, slow_table):
    session = DelayedFinancialSession(slow_table=slow_table, seconds_per_row=4)

    result, xml = collect_document(session, monkeypatch)

    assert result["status"] == "succeeded" and result["stored_source_facts"] == 101
    assert session.now - CAPTURED > TTL and session.lease_until > session.now
    assert len(session.facts) == len(session.decisions) == 101
    assert {row["record_id"] for row in session.decisions} == {row.id for row in session.facts}
    assert all(row.value.as_tuple() == Decimal(row.raw_value).as_tuple() for row in session.facts)
    assert all(row.available_at == row.captured_at == CAPTURED for row in session.facts)
    assert all(row.first_seen_at == row.extracted_at for row in session.facts)
    import hashlib
    assert session.documents[0].content_hash == hashlib.sha256(xml).hexdigest()
    original_first_seen = {row.id: row.first_seen_at for row in session.facts}
    collect_document(session, monkeypatch)
    assert len(session.facts) == len(session.decisions) == 101
    assert {row.id: row.first_seen_at for row in session.facts} == original_first_seen


def test_inventory_checks_every_node_without_spending_lease_on_full_read(monkeypatch):
    session = DelayedFinancialSession(inventory_seconds_per_row=4)

    result, _ = collect_document(session, monkeypatch)

    assert result["status"] == "succeeded" and session.lease_until > session.now
    assert session.now - CAPTURED > TTL
    assert {node for row in session.facts for node in row.evidence["source_node_ids"]} == {
        f"node-{offset}" for offset in range(101)}
    assert len(session.decisions) == 101 and session.closed_reads > 0


def test_each_storage_attempt_uses_one_full_inventory_gate(monkeypatch):
    session = DelayedFinancialSession()

    collect_document(session, monkeypatch)
    assert session.inventory_reads == 1
    collect_document(session, monkeypatch)
    assert session.inventory_reads == 2


def test_repeated_capture_checks_all_expected_parser_nodes_and_preserves_original(monkeypatch):
    from backtest_data.fundamentals.completion import parse_sec_xbrl
    session = DelayedFinancialSession()
    _, xml = collect_document(session, monkeypatch)
    parsed = parse_sec_xbrl(xml, form="10-K")
    corrupted = replace(parsed, facts=(*parsed.facts[:-1], replace(parsed.facts[-1], source_node_ids=("node-0",))))
    first_seen = {row.id: row.first_seen_at for row in session.facts}
    with pytest.raises(ValueError, match="SQL extraction inventory mismatch"):
        store_extraction(session, issuer_id="issuer", source="sec", accession="0000000001-24-000001", form="10-K",
            source_url="https://www.sec.gov/Archives/edgar/data/1/000000000124000001/example_htm.xml",
            available_at=CAPTURED, captured_at=CAPTURED, content=xml, result=corrupted, instance_key="0", fence=session.fence)
    assert len(session.documents) == 1 and len(session.facts) == len(session.decisions) == 101
    assert {row.id: row.first_seen_at for row in session.facts} == first_seen


@pytest.mark.parametrize("seconds", [290, 301])
def test_prior_read_checks_ownership_before_any_document_or_fact_write(monkeypatch, seconds):
    session = DelayedFinancialSession(prior_read_seconds=seconds, slow_table="source_financial_fact", seconds_per_row=4)
    if seconds < TTL.total_seconds():
        assert collect_document(session, monkeypatch, count=10)[0]["status"] == "succeeded"
        assert session.lease_until > session.now
    else:
        with pytest.raises(LeaseLost, match="Expired or superseded lease"):
            collect_document(session, monkeypatch, count=10)
        assert session.filings == session.documents == session.facts == session.decisions == []
        assert session.lease_until == CAPTURED + TTL


def test_expired_owner_is_rejected_before_heartbeat_autoflush_or_savepoint_flush(monkeypatch):
    session = DelayedFinancialSession(expired=True)
    pending = Issuer(market="US", source_identifier="pending", name="Pending audit evidence")
    session.add(pending)

    with pytest.raises(LeaseLost, match="Expired or superseded lease"):
        collect_document(session, monkeypatch, count=1)

    assert session.flushed == [] and session.pending == [pending]
    assert session.savepoints == 0


def test_expiry_during_orm_flush_rolls_back_nested_extraction(monkeypatch):
    session = DelayedFinancialSession(flush_seconds=301)

    with pytest.raises(LeaseLost, match="Expired or superseded lease"):
        collect_document(session, monkeypatch, count=1)

    assert session.savepoints == 1
    assert session.filings == session.documents == session.facts == session.decisions == []


def discover_page(session, monkeypatch, *, count=100):
    transactions = []

    @contextmanager
    def tx(engine):
        transactions.append(True)
        yield session

    monkeypatch.setattr(collection, "transaction", tx)
    dart_rows = [{"rcept_no": str(20260301000001 + offset), "report_nm": "사업보고서 (2025.12)"}
        for offset in range(count)]
    def dart_fetch(*args, **kwargs):
        session.provider_fetches += 1
        return None, {"list": dart_rows, "total_page": 1}
    dart = SimpleNamespace(fetch=dart_fetch)
    payload = {"filings": {"recent": {"accessionNumber": [f"0000000001-24-{offset:06d}" for offset in range(count)],
        "form": ["10-K"] * count, "filingDate": ["2024-03-01"] * count}}}
    def sec_fetch(url):
        session.provider_fetches += 1
        return SimpleNamespace(content=json.dumps(payload).encode())
    sec = SimpleNamespace(fetch=sec_fetch)
    result = collection.discover_issuer(None, issuer_id="issuer", market=session.market,
        request={"output_start": "2020-01-01", "output_end": "2026-10-11"},
        dart=dart, sec=sec, fence=session.fence)
    return result, transactions


@pytest.mark.parametrize("market", ["KR", "US"])
def test_discovery_renews_between_child_writes_and_keeps_one_page_transaction(monkeypatch, market):
    session = DelayedFinancialSession(market=market, slow_table="collection_job", seconds_per_row=4)

    result, transactions = discover_page(session, monkeypatch)

    assert result["child_filing_jobs"] == len(session.jobs) == 100
    assert session.now - CAPTURED > TTL and session.lease_until > session.now
    assert len(transactions) == 2  # issuer lookup plus the whole discovery page
    assert all(job.domain == "filing_sql" and job.request["disclosure_profile"] == "periodic_report"
        for job in session.jobs)


@pytest.mark.parametrize("seconds", [290, 301])
def test_discovery_checks_issuer_read_before_provider_or_child_writes(monkeypatch, seconds):
    session = DelayedFinancialSession(issuer_read_seconds=seconds, slow_table="collection_job", seconds_per_row=4)
    if seconds < TTL.total_seconds():
        assert discover_page(session, monkeypatch, count=10)[0]["child_filing_jobs"] == 10
        assert session.lease_until > session.now
    else:
        with pytest.raises(LeaseLost, match="Expired or superseded lease"):
            discover_page(session, monkeypatch, count=10)
        assert session.jobs == [] and session.lease_until == CAPTURED + TTL
        assert session.provider_fetches == 0
