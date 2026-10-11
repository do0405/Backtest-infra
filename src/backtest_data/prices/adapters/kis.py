"""KIS daily-itemchartprice output2 mapping with endpoint-specific raw option.

Verified official implementation and column mapping (2026-10-07):
https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_itemchartprice/inquire_daily_itemchartprice.py
https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_itemchartprice/chk_inquire_daily_itemchartprice.py
Only this endpoint's raw option (1) is supported; options are not generalized to
other KIS endpoints. Cumulative fields require a verified per-session contract.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from ..domain import PriceContext, PriceObservation
from ..normalize import ObservationMetadata, decimal_value, normalize_observation


ENDPOINT = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"


@dataclass(frozen=True, slots=True)
class KISDailyRequest:
    security_code: str
    start: date
    end: date
    market_code: str = "J"

    def __post_init__(self) -> None:
        if not self.security_code or self.end < self.start or self.market_code != "J":
            raise ValueError("approved KIS daily contract requires security code, ordered dates, and J/KRX market")

    def params(self) -> dict[str, str]:
        return {"FID_COND_MRKT_DIV_CODE": self.market_code, "FID_INPUT_ISCD": self.security_code,
                "FID_INPUT_DATE_1": self.start.strftime("%Y%m%d"), "FID_INPUT_DATE_2": self.end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1"}


def normalize_kis_bar(row: Mapping[str, Any], *, context: PriceContext, metadata: ObservationMetadata,
                      request: KISDailyRequest, response_basis: str, quantity_scope: str) -> PriceObservation:
    """Map one output2 daily row, retaining absent transaction amount and action codes.

    The caller must certify ``quantity_scope=session_total`` for this endpoint;
    an intraday cumulative quantity is not interchangeable with an interval sum.
    Reported action codes/ratios are evidence, not fabricated corporate actions.
    No exact matched VWAP eligibility is inferred from transaction amount.
    Explicit zero quantity and transaction amount with positive identical OHLC
    retain the supplier's zero-transaction quote report, anchored to raw evidence.
    This source claim does not certify a trading halt or independent no-trade fact.
    """
    if response_basis != "as_traded" or context.basis != response_basis:
        raise ValueError("response basis does not match raw request")
    if quantity_scope != "session_total":
        raise ValueError("KIS daily output requires verified session_total quantity scope")
    if context.venue != "KRX" or context.feed != "kis:krx" or context.currency != "KRW" or context.quantity_unit != "shares" or context.price_unit != "currency_per_share":
        raise ValueError("KIS context requires verified KRX/KRW/share contract")
    session = datetime.strptime(str(row["stck_bsop_date"]), "%Y%m%d").date()
    prices = tuple(decimal_value(row.get(name)) for name in ("stck_oprc", "stck_hgpr", "stck_lwpr", "stck_clpr"))
    volume, notional = decimal_value(row.get("acml_vol")), decimal_value(row.get("acml_tr_pbmn"))
    no_trade_evidence = metadata.no_trade_evidence
    if (volume == 0 and notional == 0 and all(value is not None and value > 0 for value in prices)
            and len(set(prices)) == 1):
        no_trade_evidence = f"raw:{metadata.raw_ref}#output2:{session:%Y%m%d}:acml_vol=0,acml_tr_pbmn=0"
    reported_actions = tuple((name, str(row[name])) for name in ("flng_cls_code", "prtt_rate", "mod_yn", "revl_issu_reas") if name in row)
    observation = PriceObservation(metadata.id, context, session,
        *prices, volume, notional,
        metadata.source_contract_id, metadata.raw_ref, metadata.source_revision, metadata.available_at, metadata.captured_at,
        no_trade_evidence, metadata.complete, upstream_id=metadata.upstream_id, processing_path=metadata.processing_path,
        reported_action_fields=reported_actions)
    return normalize_observation(observation, requested_basis="as_traded", requested_start=request.start, requested_end=request.end)
