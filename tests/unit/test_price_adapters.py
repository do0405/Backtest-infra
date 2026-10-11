from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from backtest_data.prices.adapters.alpaca import AlpacaDailyRequest, normalize_alpaca_bar
from backtest_data.prices.adapters.kis import KISDailyRequest, normalize_kis_bar
from backtest_data.prices.adapters.krx import normalize_krx_bar
from backtest_data.prices.normalize import ObservationMetadata
from test_corporate_actions import at
from test_price_domain import context


def metadata(**changes):
    return replace(ObservationMetadata("p1", "c1", "hash1", "r1", at(5), at(5)), **changes)


def alpaca(**changes):
    return {"t": "2024-01-04T05:00:00Z", "o": "100", "h": "110", "l": "90", "c": "105", "v": "100", "vw": "102.125", "n": 5} | changes


def test_alpaca_raw_sip_request_is_explicit_and_no_notional_is_invented():
    request = AlpacaDailyRequest(date(2024, 1, 3), date(2024, 1, 4))
    assert request.params()["feed"] == "sip"
    assert request.params()["adjustment"] == "raw"
    value = normalize_alpaca_bar(alpaca(), context=context(), metadata=metadata(), request=request,
                                 response_basis="as_traded", session_from_timestamp=lambda t: t.date())
    assert value.notional is None
    assert value.provider_vwap == D("102.125")
    assert value.volume == 100
    assert value.session == date(2024, 1, 4)


def test_alpaca_adjusted_and_outside_requested_session_rejected():
    request = AlpacaDailyRequest(date(2024, 1, 3), date(2024, 1, 4))
    with pytest.raises(ValueError, match="basis"):
        normalize_alpaca_bar(alpaca(), context=context(), metadata=metadata(), request=request,
                             response_basis="split_adjusted", session_from_timestamp=lambda t: t.date())
    with pytest.raises(ValueError, match="requested"):
        normalize_alpaca_bar(alpaca(t="2024-01-05T05:00:00Z"), context=context(), metadata=metadata(), request=request,
                             response_basis="as_traded", session_from_timestamp=lambda t: t.date())
    with pytest.raises(ValueError):
        AlpacaDailyRequest(date(2024, 1, 3), date(2024, 1, 4), adjustment="all")


def test_kis_endpoint_raw_option_and_reported_fields_are_preserved():
    request = KISDailyRequest("005930", date(2024, 1, 3), date(2024, 1, 4))
    assert request.params()["FID_ORG_ADJ_PRC"] == "1"
    assert request.params()["FID_PERIOD_DIV_CODE"] == "D"
    row = {"stck_bsop_date": "20240104", "stck_oprc": "100", "stck_hgpr": "110", "stck_lwpr": "90", "stck_clpr": "105", "acml_vol": "100", "acml_tr_pbmn": "10200", "prtt_rate": "", "flng_cls_code": "00"}
    ctx = context(venue="KRX", feed="kis:krx", currency="KRW")
    value = normalize_kis_bar(row, context=ctx, metadata=metadata(), request=request, response_basis="as_traded", quantity_scope="session_total")
    assert value.notional == 10200
    assert value.reported_action_fields == (("flng_cls_code", "00"), ("prtt_rate", ""))
    missing = normalize_kis_bar(row | {"acml_tr_pbmn": ""}, context=ctx, metadata=metadata(), request=request,
                                response_basis="as_traded", quantity_scope="session_total")
    assert missing.notional is None
    with pytest.raises(ValueError, match="scope"):
        normalize_kis_bar(row, context=ctx, metadata=metadata(), request=request, response_basis="as_traded", quantity_scope="cumulative_intraday")


def test_krx_unverified_endpoint_is_not_implemented():
    with pytest.raises(NotImplementedError, match="verified"):
        normalize_krx_bar({})
