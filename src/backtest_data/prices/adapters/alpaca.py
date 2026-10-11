"""Alpaca daily stock-bar mapping, explicitly raw/SIP and never exact VWAP.

Verified documentation (2026-10-08):
https://docs.alpaca.markets/us/reference/stockbarsingle-1
https://docs.alpaca.markets/us/docs/market-data-faq
Request/field verification does not establish account entitlement or coverage.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import re
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from ...db import utcnow
from ..domain import PriceContext, PriceObservation, require_aware
from ..normalize import ObservationMetadata, decimal_value, normalize_observation


NEW_YORK = ZoneInfo("America/New_York")
SYMBOL_PATTERN = r"[A-Z0-9][A-Z0-9.\-^$]{0,119}"


def new_york_session(timestamp: datetime) -> date:
    """Daily timestamps are the left boundary of a New York calendar day."""
    require_aware(timestamp, "source_timestamp")
    local = timestamp.astimezone(NEW_YORK)
    if local.timetz().replace(tzinfo=None) != time.min:
        raise ValueError("daily source timestamp must be New York midnight")
    return local.date()


@dataclass(frozen=True, slots=True)
class AlpacaDailyRequest:
    start: date
    end: date
    feed: str = "sip"
    adjustment: str = "raw"
    currency: str = "USD"
    symbol: str | None = None
    page_token: str | None = None
    captured_at: datetime = field(default_factory=utcnow)
    page_size: int = 10000

    def __post_init__(self) -> None:
        if self.end < self.start or self.feed != "sip" or self.adjustment != "raw" or self.currency != "USD":
            raise ValueError("approved daily contract requires ordered dates, SIP, raw, USD")
        require_aware(self.captured_at, "captured_at")
        if self.symbol is not None and not re.fullmatch(SYMBOL_PATTERN, self.symbol):
            raise ValueError("symbol must be an explicit uppercase ticker")
        if self.page_token is not None and (not isinstance(self.page_token, str) or not self.page_token or len(self.page_token) > 8192):
            raise ValueError("invalid page token")
        if isinstance(self.page_size, bool) or not isinstance(self.page_size, int) or not 1 <= self.page_size <= 10000:
            raise ValueError("page_size must be an integer from 1 to 10000")

    @property
    def effective_end(self) -> date:
        # Avoid every unclosed daily interval, including at NY midnight while
        # the prior calendar day is still inside the free SIP delay window.
        safe_day = (self.captured_at - timedelta(minutes=16)).astimezone(NEW_YORK).date() - timedelta(days=1)
        return min(self.end, safe_day)

    def endpoint(self) -> str:
        if not self.symbol or not re.fullmatch(SYMBOL_PATTERN, self.symbol):
            raise ValueError("symbol is required for the single-symbol endpoint")
        return f"/v2/stocks/{quote(self.symbol, safe='.-')}/bars"

    def params(self) -> dict[str, str]:
        if self.effective_end < self.start:
            raise ValueError("no fully ended daily interval in free historical range")
        start = datetime.combine(self.start, time.min, NEW_YORK)
        end = datetime.combine(self.effective_end + timedelta(days=1), time.min, NEW_YORK) - timedelta(microseconds=1)
        params = {"start": start.isoformat(), "end": end.isoformat(), "timeframe": "1Day",
                  "feed": self.feed, "adjustment": self.adjustment, "currency": self.currency,
                  "asof": "-", "sort": "asc", "limit": str(self.page_size)}
        if self.page_token is not None:
            params["page_token"] = self.page_token
        return params


def normalize_alpaca_bar(row: Mapping[str, Any], *, context: PriceContext,
                         metadata: ObservationMetadata, request: AlpacaDailyRequest,
                         response_basis: str,
                         session_from_timestamp: Callable[[datetime], date] = new_york_session) -> PriceObservation:
    """Map documented OHLC/v/vw fields using a contract-approved calendar mapper.

    Callers must use exact Decimal JSON decoding and confirm the response basis
    through the source contract. Session timestamps are mapped by the approved
    calendar, not guessed from local machine time. ``vw*v`` is not notional.
    """
    if response_basis != "as_traded" or context.basis != response_basis:
        raise ValueError("response basis does not match raw request")
    if context.feed != request.feed or context.currency != request.currency:
        raise ValueError("context does not match requested feed/currency")
    if context.quantity_unit != "shares" or context.price_unit != "currency_per_share":
        raise ValueError("Alpaca bar contract requires explicit share units")
    timestamp = datetime.fromisoformat(str(row["t"]).replace("Z", "+00:00"))
    require_aware(timestamp, "source_timestamp")
    session = session_from_timestamp(timestamp)
    for name in ("o", "h", "l", "c"):
        value = decimal_value(row.get(name))
        if value is None or value <= 0:
            raise ValueError("Alpaca OHLC prices must be positive")
    if decimal_value(row.get("v")) is None:
        raise ValueError("Alpaca daily volume must be explicit")
    observation = PriceObservation(metadata.id, context, session,
        decimal_value(row.get("o")), decimal_value(row.get("h")), decimal_value(row.get("l")), decimal_value(row.get("c")),
        decimal_value(row.get("v")), None, metadata.source_contract_id, metadata.raw_ref, metadata.source_revision,
        metadata.available_at, metadata.captured_at, metadata.no_trade_evidence, metadata.complete,
        provider_vwap=decimal_value(row.get("vw")), upstream_id=metadata.upstream_id,
        processing_path=metadata.processing_path, source_timestamp=timestamp)
    return normalize_observation(observation, requested_basis="as_traded", requested_start=request.start, requested_end=request.effective_end)
