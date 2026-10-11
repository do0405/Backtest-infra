from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
import importlib

import pytest


HEADER = "symbol,name,exchange,assetType,ipoDate,delistingDate,status\n"
OLD = date(2016, 1, 4)
CUTOFF = date(2026, 10, 7)


def api():
    try:
        return importlib.import_module("backtest_data.supplemental.alpha_vantage")
    except ModuleNotFoundError:
        pytest.fail("Alpha Vantage listing parser has not been implemented")


def parse(body, *, day=OLD, state="active"):
    return api().parse_listing_status(body, snapshot_date=day, state=state)


def test_builders_request_whole_active_and_delisted_lists_at_both_snapshot_dates():
    plans = api().listing_status_requests()
    assert [(dict(p.params)["date"], dict(p.params)["state"]) for p in plans] == [
        ("2026-10-07", "active"), ("2026-10-07", "delisted"),
        ("2016-01-04", "active"), ("2016-01-04", "delisted")]
    assert all(p.method == "GET" and p.url == "https://www.alphavantage.co/query" for p in plans)
    assert all(dict(p.params)["function"] == "LISTING_STATUS" for p in plans)
    assert all("symbol" not in dict(p.params) and "apikey" not in dict(p.params) for p in plans)
    with pytest.raises(FrozenInstanceError):
        plans[0].url = "changed"
    assert plans[0].request_key == api().listing_status_request(CUTOFF, "active").request_key


@pytest.mark.parametrize("day,state", [(date(2015, 12, 31), "active"), (date(2026, 10, 8), "active"), (OLD, "anything")])
def test_invalid_builder_scope_is_rejected(day, state):
    with pytest.raises(ValueError):
        api().listing_status_request(day, state)


def test_csv_dates_remain_separate_from_requested_snapshot_and_publication():
    rows = parse(HEADER + "ABC,Example Corp,NYSE,Stock,2010-01-01,2015-12-30,Delisted\n", state="delisted")
    row = rows[0]
    assert row["record_kind"] == "listing_snapshot"
    assert row["entity_key"] == "ABC"
    assert row["report_date"] == OLD
    assert row["ipo_date"] == date(2010, 1, 1)
    assert row["delisting_date"] == date(2015, 12, 30)
    assert row.get("published_date") is None
    assert row.get("effective_date") is None
    assert row["fields"]["snapshot_date"] == "2016-01-04"
    assert row["fields"]["ipoDate"] == "2010-01-01"
    assert row["fields"]["delistingDate"] == "2015-12-30"
    assert row["fields"]["historical_identity_verified"] is False
    assert "security_identity_unverified" in row["issues"]


def test_missing_dates_stay_null_and_do_not_get_snapshot_date_defaults():
    row = parse(HEADER + "ABC,Example Corp,NASDAQ,Stock,null,,Active\n")[0]
    assert row["ipo_date"] is None
    assert row["delisting_date"] is None
    assert row.get("published_date") is None
    assert row["entity_name"] == "Example Corp"
    assert row["product_type"] == "Stock"


def test_quoted_company_names_and_utf8_bom_are_supported_without_ticker_mapping():
    raw = ('\ufeff' + HEADER + 'BRK.B,"Berkshire, Class B",NYSE,Stock,1996-05-09,null,Active\n').encode()
    row = parse(raw)[0]
    assert row["entity_key"] == "BRK.B"
    assert row["entity_name"] == "Berkshire, Class B"
    assert row["fields"]["symbol"] == "BRK.B"
    assert "security_id" not in row


@pytest.mark.parametrize("payload", ['{"Information":"quota"}', '{"Note":"quota"}', '{"Error Message":"bad function"}', '{}', '[]'])
def test_json_provider_responses_never_become_successful_empty_list(payload):
    with pytest.raises(ValueError, match="provider response"):
        parse(payload)


@pytest.mark.parametrize("body", [
    "symbol,name\nABC,Issuer\n",
    HEADER + "ABC,Issuer,NYSE,Stock,2001-01-01,null,Active,extra\n",
    HEADER + "ABC,Issuer,NYSE,Stock,2001-01-01,null\n",
    HEADER + 'ABC,"Unclosed,NYSE,Stock,2001-01-01,null,Active\n',
    HEADER + 'ABC,Unquoted"Quote,NYSE,Stock,2001-01-01,null,Active\n',
    HEADER + "ABC,Issuer,NYSE,Stock,2001-02-30,null,Active\n",
    HEADER + "ABC,Issuer,NYSE,Stock,2001-01-01,2010-02-30,Active\n",
    HEADER + ",Issuer,NYSE,Stock,2001-01-01,null,Active\n",
])
def test_malformed_csv_or_source_values_fail_closed(body):
    with pytest.raises(ValueError):
        parse(body)


def test_mixed_snapshot_states_and_future_ipo_are_quarantined_individually():
    rows = parse(HEADER + "ABC,Issuer,NYSE,Stock,2001-01-01,2015-01-01,Delisted\n"
                 "XYZ,Future Issuer,NASDAQ,Stock,2020-01-01,null,Active\n"
                 "VALID,Valid Issuer,NYSE,Stock,2000-01-01,null,Active\n")
    assert len(rows) == 3
    assert rows[0]["fields"]["snapshot_scope_valid"] is False
    assert "status_snapshot_mismatch" in rows[0]["issues"]
    assert rows[1]["fields"]["snapshot_scope_valid"] is False
    assert "ipo_date_after_snapshot" in rows[1]["issues"]
    assert rows[1]["ipo_date"] == date(2020, 1, 1)
    assert rows[1]["report_date"] == OLD
    assert rows[2]["fields"]["snapshot_scope_valid"] is True
    assert all(row.get("published_date") is None for row in rows)


def test_empty_header_only_snapshot_retains_empty_result_but_does_not_prove_coverage():
    assert parse(HEADER) == []
    with pytest.raises(ValueError):
        parse("")


def test_every_source_row_is_preserved_and_snapshot_keys_do_not_collide():
    body = HEADER + "ABC,Issuer One,NYSE,Stock,2001-01-01,null,Active\nXYZ,Fund Two,NYSE ARCA,ETF,2010-01-01,null,Active\n"
    first, second = parse(body)
    assert {first["entity_key"], second["entity_key"]} == {"ABC", "XYZ"}
    assert first["row_key"] != second["row_key"]
    assert first["row_key"] != parse(body, day=CUTOFF)[0]["row_key"]


def test_datetime_snapshot_precision_is_not_silently_truncated():
    with pytest.raises(ValueError):
        api().listing_status_request(datetime(2016, 1, 4, tzinfo=timezone.utc), "active")


def test_duplicate_symbols_are_preserved_as_source_rows_without_identity_adoption():
    body = HEADER + "ABC,Issuer One,NYSE,Stock,2001-01-01,null,Active\nABC,Issuer Two,NASDAQ,Stock,2010-01-01,null,Active\n"
    rows = parse(body)
    assert len(rows) == 2
    assert rows[0]["row_key"] != rows[1]["row_key"]
    assert all("duplicate_source_symbol" in r["issues"] for r in rows)


def test_collector_builder_interface_returns_fresh_url_and_params_dict():
    request = api().listing_request(CUTOFF, "delisted")
    assert request == {"url": "https://www.alphavantage.co/query",
        "params": {"function": "LISTING_STATUS", "date": "2026-10-07", "state": "delisted"}}
    request["params"]["date"] = "changed"
    assert api().listing_request(CUTOFF, "delisted")["params"]["date"] == "2026-10-07"


def test_actual_source_descriptive_padding_and_blanks_preserve_raw_rows():
    body = HEADER + "PADDED,Issuer Name  ,NYSE,Stock,2000-01-01,null,Active\n"
    body += "MISSING,,NASDAQ,Stock,2000-01-01,null,Active\n"
    body += "EXCHANGE,Issuer,,,2000-01-01,null,Active\n"
    rows = parse(body, day=CUTOFF)
    assert len(rows) == 3
    assert rows[0]["fields"]["name"] == "Issuer Name  "
    assert "padded_name" in rows[0]["issues"]
    assert rows[1]["fields"]["name"] == ""
    assert "missing_name" in rows[1]["issues"]
    assert rows[2]["fields"]["exchange"] == ""
    assert "missing_exchange" in rows[2]["issues"]
    assert "missing_asset_type" in rows[2]["issues"]
    assert all(r["fields"]["snapshot_scope_valid"] is True for r in rows)
    assert all(r["fields"]["historical_identity_verified"] is False for r in rows)


@pytest.mark.parametrize("status", ["Unknown", "", " Active "])
def test_unreliable_status_quarantines_row_without_losing_valid_neighbors(status):
    rows = parse(HEADER + f"BAD,Issuer,NYSE,Stock,2000-01-01,null,{status}\n"
                 "VALID,Issuer,NASDAQ,Stock,2000-01-01,null,Active\n")
    assert len(rows) == 2
    assert rows[0]["fields"]["status"] == status
    assert rows[0]["fields"]["snapshot_scope_valid"] is False
    assert rows[1]["fields"]["snapshot_scope_valid"] is True


@pytest.mark.parametrize("state,ipo,delisted,issue", [
    ("active", "2000-01-01", "2015-01-01", "active_status_after_delisting_date"),
    ("delisted", "2000-01-01", "2020-01-01", "delisting_date_after_snapshot"),
    ("delisted", "2010-01-01", "2000-01-01", "delisting_date_before_ipo_date"),
])
def test_source_snapshot_date_contradictions_are_preserved_but_quarantined(state, ipo, delisted, issue):
    status = "Active" if state == "active" else "Delisted"
    row = parse(HEADER + f"ABC,Issuer,NYSE,Stock,{ipo},{delisted},{status}\n", state=state)[0]
    assert row["fields"]["snapshot_scope_valid"] is False
    assert issue in row["issues"]
    assert row["ipo_date"] == date.fromisoformat(ipo)
    assert row["delisting_date"] == date.fromisoformat(delisted)
    assert row.get("effective_date") is None
