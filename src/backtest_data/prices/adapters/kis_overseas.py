"""KIS overseas daily raw prices with explicit US venue and exact decimals.

Official request/column references (verified 2026-10-08):
https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/overseas_stock/dailyprice/dailyprice.py
https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/overseas_stock/dailyprice/chk_dailyprice.py
The overseas raw option is 0. Feed scope, turnover/volume eligibility and
historical identity are not established by the documented field names.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
import re
from typing import Any
from zoneinfo import ZoneInfo

from ...db import utcnow
from ..domain import PriceContext, PriceObservation, require_aware
from ..normalize import ObservationMetadata, decimal_value, normalize_observation
from ..numeric import exact_sum


ENDPOINT = "/uapi/overseas-price/v1/quotations/dailyprice"
TR_ID = "HHDFS76240000"
US_EXCHANGES = frozenset({"NAS", "NYS", "AMS"})
SYMBOL_PATTERN = r"[A-Z0-9][A-Z0-9./\-^$]{0,119}"
NEW_YORK = ZoneInfo("America/New_York")


def source_session(value: Any) -> date:
    """Parse the exact documented date spelling without permissive strptime."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}", value):
        raise ValueError("KIS overseas daily date must be YYYYMMDD")
    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))


def reported_precision(value: Any) -> str:
    """Validate zdiv as reporting evidence; it never rescales supplied prices."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,5}", value) or int(value) > 10000:
        raise ValueError("KIS reported decimal precision must be a nonnegative integer")
    return value


@dataclass(frozen=True, slots=True)
class KISOverseasDailyRequest:
    symbol: str
    start: date
    end: date
    exchange: str
    captured_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not re.fullmatch(SYMBOL_PATTERN, self.symbol):
            raise ValueError("symbol must be an exact safe uppercase provider symbol")
        if not isinstance(self.exchange, str) or self.exchange not in US_EXCHANGES:
            raise ValueError("KIS US daily contract requires NAS, NYS or AMS exchange")
        if type(self.start) is not date or type(self.end) is not date or self.end < self.start:
            raise ValueError("KIS daily request requires ordered calendar dates")
        if not isinstance(self.captured_at, datetime):
            raise ValueError("captured_at requires a timezone-aware timestamp")
        require_aware(self.captured_at, "captured_at")
        # Conservatively require a fully ended New York calendar day. A market
        # close is not a verified publication/revision deadline for this feed.
        if self.end >= self.captured_at.astimezone(NEW_YORK).date():
            raise ValueError("KIS daily end must precede the current New York day")

    @property
    def effective_end(self) -> date:
        return self.end

    def endpoint(self) -> str:
        return ENDPOINT

    def params(self) -> dict[str, str]:
        return {"AUTH": "", "EXCD": self.exchange, "SYMB": self.symbol,
                "GUBN": "0", "BYMD": f"{self.end.year:04d}{self.end.month:02d}{self.end.day:02d}", "MODP": "0"}


def normalize_kis_overseas_bar(row: Mapping[str, Any], *, context: PriceContext,
                               metadata: ObservationMetadata, request: KISOverseasDailyRequest,
                               response_basis: str = "as_traded",
                               quantity_scope: str = "session_total") -> PriceObservation:
    """Preserve documented prices, share volume and supplier transaction amount.

    ``session_total`` identifies the endpoint's daily quantity field. Its trade
    eligibility and matching with ``tamt`` remain unverified, so the observation
    does not certify an exact VWAP. Decimal strings are already currency prices;
    ``zdiv`` is reporting metadata, not an instruction to divide those prices.
    """
    if response_basis != "as_traded" or context.basis != response_basis:
        raise ValueError("response basis does not match overseas raw request")
    if quantity_scope != "session_total" or context.session_scope != "session_total":
        raise ValueError("KIS overseas daily quantity field requires session_total scope")
    if (context.venue != request.exchange or context.feed != "kis:us:" + request.exchange
            or context.currency != "USD" or context.quantity_unit != "shares"
            or context.price_unit != "currency_per_share"):
        raise ValueError("KIS overseas context requires explicit US venue/USD/share contract")
    if not isinstance(row, Mapping):
        raise ValueError("KIS daily bar must be an object")
    session = source_session(row.get("xymd"))
    if metadata.available_at != metadata.captured_at:
        raise ValueError("KIS overseas availability requires capture-only evidence")
    if session >= metadata.captured_at.astimezone(NEW_YORK).date():
        raise ValueError("KIS overseas daily source session must have fully ended")
    if "zdiv" in row:
        reported_precision(row["zdiv"])
    prices = {name: decimal_value(row.get(name)) for name in ("open", "high", "low", "clos")}
    if any(value is None or value <= 0 for value in prices.values()):
        raise ValueError("KIS overseas OHLC prices must be positive")
    volume = decimal_value(row.get("tvol"))
    if volume is None:
        raise ValueError("KIS overseas daily volume must be explicit")
    notional = decimal_value(row.get("tamt"))
    for value in (*prices.values(), volume, notional):
        if value is not None:
            # A singleton exact sum checks the shared supported numeric envelope
            # while preserving the original coefficient and exponent unchanged.
            exact_sum((value,))
    observation = PriceObservation(metadata.id, context, session,
        prices["open"], prices["high"], prices["low"], prices["clos"], volume, notional,
        metadata.source_contract_id, metadata.raw_ref, metadata.source_revision,
        metadata.available_at, metadata.captured_at, metadata.no_trade_evidence, metadata.complete,
        upstream_id=metadata.upstream_id, processing_path=metadata.processing_path)
    return normalize_observation(observation, requested_basis="as_traded",
        requested_start=request.start, requested_end=request.end)
