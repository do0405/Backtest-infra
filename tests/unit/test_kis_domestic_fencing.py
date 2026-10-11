"""Domestic storage keeps one-row inserts and the caller's strict lease."""
from datetime import date, timedelta
import inspect
import json

import pytest

from backtest_data.ingestion.http import ProviderResponse
from backtest_data.ingestion.jobs import LeaseLost
from backtest_data.prices.adapters.kis import KISDailyRequest
from backtest_data.prices.storage import store_kis_prices

from test_price_storage_fencing import CAPTURED, TTL, DelayedSession


def page(count=101, *, rejected=False):
    rows = [{"stck_bsop_date": (date(2024, 7, 9) - timedelta(days=index)).strftime("%Y%m%d"),
        "stck_oprc": "100.0100", "stck_hgpr": "110", "stck_lwpr": "90",
        "stck_clpr": "NaN" if rejected else "105", "acml_vol": "100", "acml_tr_pbmn": "10212.5000"}
        for index in range(count)]
    payload = {"rt_cd": "0", "output2": rows}
    return KISDailyRequest("005930", date(2024, 1, 1), date(2024, 7, 9)), payload


def store(session, raw_root, *, count=101, rejected=False, completion_check=True):
    request, payload = page(count, rejected=rejected)
    response = ProviderResponse(json.dumps(payload).encode(), 200, CAPTURED, "application/json")
    session.fence()
    # Before the optional callback exists, reproduce the existing caller's
    # beginning/final lease checks to expose cumulative expiry in real code.
    kwargs = {"fence": session.fence} if "fence" in inspect.signature(store_kis_prices).parameters else {}
    result = store_kis_prices(session, "security", request, response, payload, raw_root, **kwargs)
    if completion_check:
        session.fence()
    return result


@pytest.mark.parametrize("slow_table", ["price_observation", "selection_decision"])
def test_domestic_page_survives_cumulative_slow_one_row_writes(slow_table, tmp_path):
    session = DelayedSession(market="KR", slow_table=slow_table, seconds_per_row=4)

    result = store(session, tmp_path)

    assert result["inserted"] == result["source_row_count"] == 101
    assert session.now - CAPTURED > TTL
    assert session.lease_until > session.now
    assert all(size == 1 for table, size in session.writes if table == "price_observation")
    assert {row["record_id"] for row in session.decisions} == {row["id"] for row in session.prices}


def test_domestic_refreshes_between_two_slow_prior_reads(tmp_path):
    session = DelayedSession(market="KR", prior_read_seconds=160)

    result = store(session, tmp_path, count=1)

    assert result["inserted"] == 1
    assert session.now - CAPTURED == timedelta(seconds=320)
    assert session.lease_until > session.now


def test_domestic_expired_first_read_rejects_before_price_write(tmp_path):
    session = DelayedSession(market="KR", prior_read_seconds=301)

    with pytest.raises(LeaseLost):
        store(session, tmp_path, count=1)

    assert session.prices == session.decisions == []
    assert session.lease_until == CAPTURED + TTL


def test_domestic_rejected_evidence_flushes_in_bounded_valid_lease_batches(tmp_path):
    session = DelayedSession(market="KR", issue_seconds_per_row=4)

    result = store(session, tmp_path, rejected=True)

    assert result["rejected"] == len(session.issues) == 101
    assert session.now - CAPTURED > TTL
    assert session.lease_until > session.now
    assert max(session.issue_flush_sizes) <= 10
    assert {issue.evidence["row_index"] for issue in session.issues} == set(range(101))


def test_domestic_storage_rejects_expired_evidence_flush_itself(tmp_path):
    session = DelayedSession(market="KR", issue_seconds_per_row=301)

    with pytest.raises(LeaseLost):
        store(session, tmp_path, count=1, rejected=True, completion_check=False)


def test_domestic_metadata_steps_refresh_between_individually_valid_stalls(tmp_path):
    session = DelayedSession(market="KR", metadata_seconds={"source_contract": 160, "raw_object": 160, "raw_read": 160})

    result = store(session, tmp_path, count=1)

    assert result["inserted"] == 1
    assert session.now - CAPTURED == timedelta(seconds=480)
    assert session.lease_until > session.now


def test_domestic_archive_put_gets_a_fresh_lease_before_raw_insert(tmp_path, monkeypatch):
    from backtest_data.prices.storage import RawStore
    session = DelayedSession(market="KR", metadata_seconds={"source_contract": 160})
    original_put = RawStore.put

    def delayed_put(archive, content):
        result = original_put(archive, content)
        session.now += timedelta(seconds=160)
        return result

    monkeypatch.setattr(RawStore, "put", delayed_put)

    assert store(session, tmp_path, count=1)["inserted"] == 1
    assert session.now - CAPTURED == timedelta(seconds=320)
    assert session.lease_until > session.now
