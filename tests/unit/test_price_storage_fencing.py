"""Exercise real storage/heartbeat code with deterministic PostgreSQL delays.

The session double compiles the actual PostgreSQL statements; no production
database, provider calls, or wall-clock waits are involved.
"""
from datetime import date, datetime, timedelta, timezone
from contextlib import contextmanager
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from backtest_data.catalog.models import Security, SourceContract
from backtest_data.ingestion.http import ProviderResponse
from backtest_data.ingestion.jobs import JobLease, LeaseLost, heartbeat_job
from backtest_data.ingestion.models import RawObject
from backtest_data.ingestion.raw_store import RawStore
from backtest_data.prices.adapters.alpaca import AlpacaDailyRequest, NEW_YORK
from backtest_data.prices.adapters.kis_overseas import KISOverseasDailyRequest
from backtest_data.prices.alpaca_storage import store_alpaca_prices
from backtest_data.prices.kis_overseas_storage import store_kis_overseas_prices
from backtest_data.prices.models import PriceObservation
from backtest_data.releases.models import SelectionDecision, ValidationIssue


CAPTURED = datetime(2024, 7, 10, 18, tzinfo=timezone.utc)
TTL = timedelta(minutes=5)
LEASE = JobLease("job", "collector", 7)


class DelayedSession:
    def __init__(self, *, slow_table=None, seconds_per_row=0, prior_read_seconds=0, flush_seconds=0,
                 issue_seconds_per_row=0, market="US", metadata_seconds=None):
        self.now = CAPTURED
        self.lease_until = self.now + TTL
        self.slow_table = slow_table
        self.seconds_per_row = seconds_per_row
        self.prior_read_seconds = prior_read_seconds
        self.flush_seconds = flush_seconds
        self.issue_seconds_per_row = issue_seconds_per_row
        self.issues = []
        self.issue_flush_sizes = []
        self.market = market
        self.metadata_seconds = metadata_seconds or {}
        self.autoflush = True
        self.prices = []
        self.decisions = []
        self.pending = []
        self.writes = []
        self.heartbeats = []
        self.contract = None
        self.raw = None

    def fence(self):
        heartbeat_job(self, LEASE, self.now, TTL)

    @property
    @contextmanager
    def no_autoflush(self):
        previous = self.autoflush
        self.autoflush = False
        try:
            yield
        finally:
            self.autoflush = previous

    def _autoflush(self):
        if self.autoflush and self.pending:
            self.flush()

    def execute(self, statement):
        if statement.is_select:
            self._autoflush()
            assert statement.column_descriptions[0]["entity"] is Security
            security = SimpleNamespace(id="security", market=self.market, source_identifier="nasdaq_directory:AAPL")
            return SimpleNamespace(scalar_one=lambda: security)
        params = statement.compile(dialect=postgresql.dialect()).params
        self._autoflush()
        if statement.is_update:
            assert statement.table.name == "collection_job"
            matched = (params["id_1"] == LEASE.job_id and params["lease_owner_1"] == LEASE.owner
                and params["fencing_token_1"] == LEASE.fencing_token and params["status_1"] == "running"
                and self.lease_until > params["lease_until_1"])
            if matched:
                self.lease_until = params["lease_until"]
                self.heartbeats.append(self.now)
            return SimpleNamespace(rowcount=int(matched))
        assert statement.is_insert
        table = statement.table.name
        if table in {"price_observation", "selection_decision"}:
            indexes = sorted(int(key.removeprefix("id_m")) for key in params if key.startswith("id_m"))
            rows = [{column.name: params.get(f"{column.name}_m{index}") for column in statement.table.columns}
                for index in indexes]
            if not indexes:
                rows = [{column.name: params.get(column.name) for column in statement.table.columns}]
            self.writes.append((table, len(rows)))
            if self.slow_table == table:
                self.now += timedelta(seconds=self.seconds_per_row * len(rows))
            if table == "price_observation":
                signatures = {row["signature"] for row in self.prices}
                returned = []
                for row in rows:
                    if row["signature"] not in signatures:
                        self.prices.append(row)
                        signatures.add(row["signature"])
                        returned.append(row["id"])
                return SimpleNamespace(all=lambda: returned, scalar_one_or_none=lambda: returned[0] if returned else None)
            self.decisions.extend(rows)
        elif table == "source_contract":
            self.now += timedelta(seconds=self.metadata_seconds.get(table, 0))
            self.contract = self.contract or SimpleNamespace(**params)
        elif table == "raw_object":
            self.now += timedelta(seconds=self.metadata_seconds.get(table, 0))
            self.raw = SimpleNamespace(**params)
        else:
            raise AssertionError(f"Unexpected table: {table}")
        return SimpleNamespace()

    def scalar(self, statement):
        self._autoflush()
        entity = statement.column_descriptions[0]["entity"]
        if entity is PriceObservation:
            selected = self._previous(statement)
            return selected[0] if selected else None
        assert entity in {SourceContract, RawObject}
        if entity is RawObject:
            self.now += timedelta(seconds=self.metadata_seconds.get("raw_read", 0))
        return self.contract if entity is SourceContract else self.raw

    def scalars(self, statement):
        if statement.is_insert:
            return self.execute(statement)
        selected = self._previous(statement)
        return ScalarRows(selected)

    def _previous(self, statement):
        self._autoflush()
        assert statement.column_descriptions[0]["entity"] is PriceObservation
        params = statement.compile(dialect=postgresql.dialect()).params
        self.now += timedelta(seconds=self.prior_read_seconds)
        selected = [SimpleNamespace(**row) for row in reversed(self.prices)
            if row["session"] in (params["session_1"] if isinstance(params["session_1"], list) else [params["session_1"]])
            and row["captured_at"] <= params.get("captured_at_1", row["captured_at"])]
        return selected

    def add(self, record):
        self.pending.append(record)

    def flush(self):
        issues = [record for record in self.pending if isinstance(record, ValidationIssue)]
        decisions = [record for record in self.pending if isinstance(record, SelectionDecision)]
        self.decisions.extend({column.name: getattr(record, column.name, None)
            for column in SelectionDecision.__table__.columns} for record in decisions)
        if issues:
            self.issue_flush_sizes.append(len(issues))
            self.issues.extend(issues)
        self.now += timedelta(seconds=self.flush_seconds + self.issue_seconds_per_row * len(issues))
        if self.slow_table == "selection_decision":
            self.now += timedelta(seconds=self.seconds_per_row * len(decisions))
        self.pending.clear()


class ScalarRows(list):
    def all(self):
        return list(self)


def page(provider, count=101):
    days = [date(2024, 7, 9) - timedelta(days=index) for index in range(count)]
    if provider == "alpaca":
        request = AlpacaDailyRequest(date(2024, 1, 1), date(2024, 7, 9), symbol="AAPL", captured_at=CAPTURED)
        rows = [{"t": datetime.combine(day, datetime.min.time(), NEW_YORK).isoformat(),
            "o": "100.0100", "h": "110", "l": "90", "c": "105", "v": 100} for day in days]
        return store_alpaca_prices, request, {"symbol": "AAPL", "bars": rows, "next_page_token": "next-page"}, {}
    request = KISOverseasDailyRequest("AAPL", date(2024, 1, 1), date(2024, 7, 9), "NAS", captured_at=CAPTURED)
    rows = [{"xymd": day.strftime("%Y%m%d"), "open": "100.0100", "high": "110", "low": "90",
        "clos": "105", "tvol": "100", "tamt": "10212.5000"} for day in days]
    mapping = {"market": "US", "security_id": "security", "provider_symbol": "AAPL", "exchange": "NAS",
        "source": "kis_overseas_master", "qualified_current_mapping": True,
        "source_directory_hash": "a" * 64, "realtime_symbol": "DNASAAPL"}
    return store_kis_overseas_prices, request, {"rt_cd": "0", "output1": {"rsym": "DNASAAPL", "zdiv": "4"},
        "output2": rows}, {"mapping_evidence": mapping}


def store_page(session, provider, raw_root, *, count=101):
    store, request, payload, arguments = page(provider, count)
    response = ProviderResponse(json.dumps(payload).encode(), 200, CAPTURED, "application/json")
    result = store(session, "security", request, response, payload, raw_root, fence=session.fence, **arguments)
    assert RawStore(raw_root).read(session.raw.storage_key) == response.content
    return result


@pytest.mark.parametrize("provider", ["alpaca", "kis_overseas"])
@pytest.mark.parametrize("slow_table", ["price_observation", "selection_decision"])
def test_page_survives_slow_writes_without_spending_five_minute_lease(provider, slow_table, tmp_path):
    # Four seconds/row means a 100-row statement expires a strict five-minute
    # lease; ten-row statements leave time for a valid heartbeat on both sides.
    session = DelayedSession(slow_table=slow_table, seconds_per_row=4)

    result = store_page(session, provider, tmp_path)

    assert result["inserted"] == result["source_row_count"] == 101
    assert session.now - CAPTURED > TTL  # whole pages can take longer than TTL
    assert session.lease_until > session.now
    assert {row["record_id"] for row in session.decisions} == {row["id"] for row in session.prices}
    assert len(session.decisions) == len(session.prices) == 101
    assert all(row["open"].as_tuple() == Decimal("100.0100").as_tuple() for row in session.prices)
    assert all(row["available_at"] == row["captured_at"] == CAPTURED for row in session.prices)
    assert all(row["evidence"]["source_row_hash"] for row in session.prices)
    assert store_page(session, provider, tmp_path)["inserted"] == 0
    assert len(session.decisions) == len(session.prices) == 101


@pytest.mark.parametrize("provider", ["alpaca", "kis_overseas"])
def test_slow_prior_read_refreshes_valid_lease_before_first_write(provider, tmp_path):
    session = DelayedSession(slow_table="price_observation", seconds_per_row=4, prior_read_seconds=290)

    result = store_page(session, provider, tmp_path, count=10)

    assert result["inserted"] == 10
    assert session.lease_until > session.now


@pytest.mark.parametrize("provider", ["alpaca", "kis_overseas"])
def test_expired_prior_read_rejects_page_before_any_price_write(provider, tmp_path):
    session = DelayedSession(prior_read_seconds=301)

    with pytest.raises(LeaseLost, match="Expired or superseded lease"):
        store_page(session, provider, tmp_path, count=10)

    assert session.prices == session.decisions == []
    assert session.lease_until == CAPTURED + TTL


@pytest.mark.parametrize("provider", ["alpaca", "kis_overseas"])
def test_final_flush_does_not_hide_expired_lease(provider, tmp_path):
    session = DelayedSession(flush_seconds=301)

    with pytest.raises(LeaseLost, match="Expired or superseded lease"):
        store_page(session, provider, tmp_path, count=0)


@pytest.mark.parametrize("provider", ["alpaca", "kis_overseas"])
def test_rejection_heavy_page_flushes_evidence_without_expiring_lease(provider, tmp_path):
    session = DelayedSession(issue_seconds_per_row=4)
    store, request, payload, arguments = page(provider)
    rows = payload["bars" if provider == "alpaca" else "output2"]
    for row in rows:
        row["c" if provider == "alpaca" else "clos"] = "NaN"
    response = ProviderResponse(json.dumps(payload).encode(), 200, CAPTURED, "application/json")

    result = store(session, "security", request, response, payload, tmp_path, fence=session.fence, **arguments)

    assert result["rejected"] == result["source_row_count"] == len(session.issues) == 101
    assert result["inserted"] == 0
    assert session.now - CAPTURED > TTL
    assert session.lease_until > session.now
    assert {issue.evidence["row_index"] for issue in session.issues} == set(range(101))
    assert max(session.issue_flush_sizes) <= 10


@pytest.mark.parametrize("provider", ["alpaca", "kis_overseas"])
def test_metadata_steps_refresh_between_individually_valid_stalls(provider, tmp_path):
    session = DelayedSession(metadata_seconds={"source_contract": 160, "raw_object": 160, "raw_read": 160})

    result = store_page(session, provider, tmp_path, count=1)

    assert result["inserted"] == 1
    assert session.now - CAPTURED == timedelta(seconds=480)
    assert session.lease_until > session.now


@pytest.mark.parametrize("provider", ["alpaca", "kis_overseas"])
def test_archive_put_refreshes_valid_owner_before_raw_insert(provider, tmp_path, monkeypatch):
    session = DelayedSession(metadata_seconds={"source_contract": 160})
    original_put = RawStore.put

    def delayed_put(archive, content):
        result = original_put(archive, content)
        session.now += timedelta(seconds=160)
        return result

    monkeypatch.setattr(RawStore, "put", delayed_put)

    assert store_page(session, provider, tmp_path, count=1)["inserted"] == 1
    assert session.now - CAPTURED == timedelta(seconds=320)
    assert session.lease_until > session.now


@pytest.mark.parametrize("provider", ["alpaca", "kis_overseas"])
def test_expired_contract_step_rejects_before_archiving_or_price_write(provider, tmp_path):
    session = DelayedSession(metadata_seconds={"source_contract": 301})

    with pytest.raises(LeaseLost):
        store_page(session, provider, tmp_path, count=1)

    assert session.raw is None
    assert session.prices == session.decisions == []
    assert session.lease_until == CAPTURED + TTL


@pytest.mark.parametrize("provider", ["alpaca", "kis_overseas"])
def test_pending_rejection_cannot_autoflush_through_fence_or_prior_read(provider, tmp_path):
    session = DelayedSession(prior_read_seconds=290, issue_seconds_per_row=10)
    store, request, payload, arguments = page(provider, count=11)
    payload["bars" if provider == "alpaca" else "output2"][0]["c" if provider == "alpaca" else "clos"] = "NaN"
    response = ProviderResponse(json.dumps(payload).encode(), 200, CAPTURED, "application/json")

    result = store(session, "security", request, response, payload, tmp_path, fence=session.fence, **arguments)

    assert result["inserted"] == 10 and result["rejected"] == 1
    assert session.lease_until > session.now
    assert session.issue_flush_sizes == [1]
