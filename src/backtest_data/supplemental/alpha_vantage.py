"""Whole-list Alpha Vantage LISTING_STATUS snapshots, without identity adoption.

This module performs no I/O and accepts no API credentials. A snapshot's
requested date is separate from provider IPO/delisting dates. None of those
dates proves historical dissemination or a security's last trading session.
"""

from collections.abc import Sequence
import csv
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from io import StringIO
import re


START_DATE = date(2016, 1, 1)
END_DATE = date(2026, 10, 7)
SNAPSHOT_DATES = (END_DATE, date(2016, 1, 4))
STATES = ("active", "delisted")
URL = "https://www.alphavantage.co/query"
CSV_FIELDS = ("symbol", "name", "exchange", "assetType", "ipoDate", "delistingDate", "status")


def _scope(snapshot_date: date, state: str) -> None:
    if type(snapshot_date) is not date or not START_DATE <= snapshot_date <= END_DATE:
        raise ValueError("listing snapshot must be an exact date within the configured period")
    if state not in STATES:
        raise ValueError("listing state must be active or delisted")


@dataclass(frozen=True, slots=True)
class AlphaVantageRequestSpec:
    method: str
    url: str
    params: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        params = tuple(tuple(pair) for pair in self.params)
        if any(len(pair) != 2 or not all(isinstance(v, str) for v in pair) for pair in params):
            raise ValueError("request parameters must be immutable string pairs")
        object.__setattr__(self, "params", params)
        values = dict(params)
        if (self.method != "GET" or self.url != URL or len(values) != len(params)
                or set(values) != {"function", "date", "state"}
                or values["function"] != "LISTING_STATUS"):
            raise ValueError("only credential-free whole-list LISTING_STATUS requests are allowed")
        _scope(date.fromisoformat(values["date"]), values["state"])

    @property
    def request_key(self) -> str:
        return sha256(repr((self.method, self.url, tuple(sorted(self.params)))).encode()).hexdigest()


def listing_status_request(snapshot_date: date, state: str) -> AlphaVantageRequestSpec:
    """Build one whole-list request; transport supplies credentials separately."""
    _scope(snapshot_date, state)
    return AlphaVantageRequestSpec("GET", URL, (("function", "LISTING_STATUS"),
        ("date", snapshot_date.isoformat()), ("state", state)))


def listing_request(day: date, state: str) -> dict:
    """Collector adapter with fresh public URL/parameter dictionaries."""
    spec = listing_status_request(day, state)
    return {"url": spec.url, "params": dict(spec.params)}


def listing_status_requests(snapshot_dates: Sequence[date] = SNAPSHOT_DATES,
                            states: Sequence[str] = STATES) -> tuple[AlphaVantageRequestSpec, ...]:
    """Default: active and delisted snapshots for 2016-01-04/2026-10-07."""
    dates, scopes = tuple(snapshot_dates), tuple(states)
    if not dates or not scopes or len(set(dates)) != len(dates) or len(set(scopes)) != len(scopes):
        raise ValueError("listing request dates/states must be nonempty and unique")
    return tuple(listing_status_request(day, state) for day in dates for state in scopes)


def _validate_csv_quotes(text: str) -> None:
    """Reject quote characters in unquoted fields that csv.strict permits."""
    in_quotes = after_quote = False
    field_start = True
    position = 0
    while position < len(text):
        char = text[position]
        if char == "\x00":
            raise ValueError("listing CSV contains a NUL character")
        if in_quotes:
            if char == '"':
                if position + 1 < len(text) and text[position + 1] == '"':
                    position += 1
                else:
                    in_quotes, after_quote = False, True
        elif after_quote:
            if char not in ",\r\n":
                raise ValueError("listing CSV has characters after a closing quote")
            after_quote, field_start = False, True
        elif char == '"':
            if not field_start:
                raise ValueError("listing CSV contains an unquoted quote")
            in_quotes, field_start = True, False
        elif char in ",\r\n":
            field_start = True
        else:
            field_start = False
        position += 1
    if in_quotes:
        raise ValueError("listing CSV has an unclosed quoted field")


def _source_date(value: str) -> date | None:
    if value == "" or value.lower() == "null":
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("listing dates must be ISO source dates or null")
    return date.fromisoformat(value)


def parse_listing_status(content: bytes | str, *, snapshot_date: date,
                         state: str) -> list[dict]:
    """Strict CSV into source snapshot rows; errors never mean an empty list.

    `ipo_date`/`delisting_date` are source date values, also preserved in fields
    for the generic SQL staging contract. `report_date` labels the requested
    snapshot only. No publication date, canonical ID, or coverage is inferred.
    Repeated symbols remain separate raw source rows with an ambiguity issue.
    Missing/padded descriptions and contradictory snapshot metadata quarantine
    individual rows; malformed CSV/dates or missing symbols reject the response.
    """
    _scope(snapshot_date, state)
    if isinstance(content, bytes):
        text = content.decode("utf-8-sig", errors="strict")
    elif isinstance(content, str):
        text = content.removeprefix("\ufeff")
    else:
        raise ValueError("listing response must be UTF-8 bytes or text")
    if text.lstrip().startswith(("{", "[")):
        # Includes Information, Note, Error Message, and unknown JSON schemas.
        # Never echo the provider body into an exception/log.
        raise ValueError("Alpha Vantage provider response is JSON rather than listing CSV")
    _validate_csv_quotes(text)
    reader = csv.reader(StringIO(text, newline=""), strict=True)
    try:
        header = next(reader, None)
        if header != list(CSV_FIELDS):
            raise ValueError("Alpha Vantage listing CSV schema changed or is missing")
        result, by_symbol = [], {}
        for source_row, values in enumerate(reader, 1):
            if not values:
                continue
            if len(values) != len(CSV_FIELDS):
                raise ValueError("Alpha Vantage listing CSV record width changed")
            raw = dict(zip(CSV_FIELDS, values, strict=True))
            if not raw["symbol"].strip() or raw["symbol"] != raw["symbol"].strip():
                raise ValueError("listing source symbol is missing or padded")
            ipo, delisted = _source_date(raw["ipoDate"]), _source_date(raw["delistingDate"])
            issues = ["security_identity_unverified", "public_dissemination_time_unverified",
                      "last_trading_session_unverified", "whole_list_coverage_unverified"]
            scope_valid = True
            for field, label in (("name", "name"), ("exchange", "exchange"), ("assetType", "asset_type")):
                if not raw[field].strip():
                    issues.append(f"missing_{label}")
                if raw[field] != raw[field].strip():
                    issues.append(f"padded_{label}")
            source_state = raw["status"].lower()
            if raw["status"] != raw["status"].strip():
                issues.append("padded_source_status")
                scope_valid = False
            if source_state not in STATES:
                issues.append("source_status_unknown")
                scope_valid = False
            elif source_state != state:
                issues.append("status_snapshot_mismatch")
                scope_valid = False
            if ipo is not None and ipo > snapshot_date:
                issues.append("ipo_date_after_snapshot")
                scope_valid = False
            if ipo is not None and delisted is not None and delisted < ipo:
                issues.append("delisting_date_before_ipo_date")
                scope_valid = False
            if state == "delisted" and delisted is not None and delisted > snapshot_date:
                issues.append("delisting_date_after_snapshot")
                scope_valid = False
            if state == "active" and delisted is not None and delisted <= snapshot_date:
                issues.append("active_status_after_delisting_date")
                scope_valid = False
            if delisted is not None and delisted > snapshot_date:
                issues.append("future_delisting_date")
            if state == "delisted" and delisted is None:
                issues.append("delisting_date_unknown")
            fields = raw | {"snapshot_date": snapshot_date.isoformat(), "requested_state": state,
                "ipo_date": ipo.isoformat() if ipo else None,
                "delisting_date": delisted.isoformat() if delisted else None,
                "historical_identity_verified": False, "publication_verified": False,
                "whole_list_snapshot": True, "snapshot_scope_valid": scope_valid}
            row = dict(record_kind="listing_snapshot", entity_key=raw["symbol"],
                row_key=f"{snapshot_date.isoformat()}:{state}:{source_row}:{raw['symbol']}",
                entity_name=raw["name"] or None, product_type=raw["assetType"] or None, report_date=snapshot_date,
                ipo_date=ipo, delisting_date=delisted, published_date=None, effective_date=None,
                date_basis="requested_asof_listing_snapshot", fields=fields, issues=issues,
                canonical_eligible=False)
            previous = by_symbol.setdefault(raw["symbol"], [])
            if previous:
                for duplicate in previous + [row]:
                    if "duplicate_source_symbol" not in duplicate["issues"]:
                        duplicate["issues"].append("duplicate_source_symbol")
            previous.append(row)
            result.append(row)
        return result
    except csv.Error:
        raise ValueError("Alpha Vantage listing CSV is malformed") from None
