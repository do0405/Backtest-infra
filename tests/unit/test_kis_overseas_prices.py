from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
import importlib

import pytest

from backtest_data.prices.domain import PriceContext
from backtest_data.prices.normalize import ObservationMetadata


def adapter():
    name = "backtest_data.prices.adapters.kis_overseas"
    assert importlib.util.find_spec(name) is not None, "KIS US daily adapter is required"
    return importlib.import_module(name)


def request(**changes):
    return adapter().KISOverseasDailyRequest("AAPL", date(2024, 1, 1), date(2024, 7, 9), "NAS",
        captured_at=datetime(2024, 7, 10, 18, tzinfo=timezone.utc), **changes)


def bar(**changes):
    return {"xymd": "20240709", "open": "100.0100", "high": "110.1200", "low": "90.0000",
            "clos": "105.0100", "tvol": "100", "tamt": "10212.5000"} | changes


def normalized(row=None, **changes):
    value = request()
    context = PriceContext("s", "s:NAS", "NAS", "kis:us:NAS", "session_total", "USD",
        "currency_per_share", "shares", "as_traded")
    metadata = ObservationMetadata("p", "c", "raw", "rev", value.captured_at, value.captured_at)
    arguments = {"context": context, "metadata": metadata, "request": value} | changes
    return adapter().normalize_kis_overseas_bar(row or bar(), **arguments)


def test_request_uses_static_us_endpoint_and_overseas_raw_option_zero():
    value = request()
    assert value.endpoint() == "/uapi/overseas-price/v1/quotations/dailyprice"
    assert value.params() == {"AUTH": "", "EXCD": "NAS", "SYMB": "AAPL", "GUBN": "0",
                              "BYMD": "20240709", "MODP": "0"}
    assert value.effective_end == value.end


@pytest.mark.parametrize("symbol", ["BRK/B", "BRK.B", "BAC$B", "BAC^B", "A-B", "0123"])
def test_provider_symbol_punctuation_and_leading_zero_are_preserved_exactly(symbol):
    value = replace(request(), symbol=symbol)
    assert value.params()["SYMB"] == symbol
    assert value.endpoint() == request().endpoint()


@pytest.mark.parametrize("symbol", ["../orders", "AAPL?feed=x", "aapl", "", "AAPL\\x", "AAPL\n", "AAPL&x=y", "A" * 121])
def test_symbol_rejects_control_or_query_characters(symbol):
    with pytest.raises(ValueError, match="symbol"):
        replace(request(), symbol=symbol)


@pytest.mark.parametrize("changes", [
    {"exchange": "NYSE"}, {"exchange": "HKS"}, {"start": date(2024, 7, 10)},
    {"start": datetime(2024, 1, 1, tzinfo=timezone.utc)}, {"end": "20240709"},
    {"captured_at": datetime(2024, 7, 10)}, {"end": date(2024, 7, 10)},
    {"end": date(2024, 7, 11)},
])
def test_request_rejects_non_us_venue_bad_dates_and_unclosed_new_york_days(changes):
    with pytest.raises(ValueError):
        replace(request(), **changes)


def test_request_new_york_date_uses_aware_capture_clock():
    with pytest.raises(ValueError):
        replace(request(), end=date(2024, 7, 9), captured_at=datetime(2024, 7, 10, 2, tzinfo=timezone.utc))
    assert replace(request(), captured_at=datetime(2024, 7, 10, 4, tzinfo=timezone.utc)).end == date(2024, 7, 9)


def test_request_formats_four_digit_year_without_platform_strftime_loss():
    value = replace(request(), start=date(1, 1, 1), end=date(1, 1, 1))
    assert value.params()["BYMD"] == "00010101"


def test_normalizer_preserves_reported_decimal_prices_and_turnover_without_zdiv_scaling():
    with localcontext() as ctx:
        ctx.prec = 3
        result = normalized(bar(zdiv="4"))
    assert result.open.as_tuple() == Decimal("100.0100").as_tuple()
    assert result.close == Decimal("105.0100")
    assert result.volume == Decimal("100")
    assert result.notional.as_tuple() == Decimal("10212.5000").as_tuple()
    assert result.provider_vwap is None
    assert result.available_at == result.captured_at
    assert result.context.feed == "kis:us:NAS"


def test_absent_transaction_amount_stays_unknown():
    result = normalized(bar(tamt=""))
    assert result.notional is None


@pytest.mark.parametrize("changes", [
    {"xymd": "202479"}, {"xymd": "20240230"}, {"xymd": "20240710"}, {"xymd": "20231231"},
    {"open": 100.01}, {"open": True}, {"clos": "NaN"}, {"high": "Infinity"},
    {"open": "0"}, {"low": "-1"}, {"low": "111"}, {"tvol": "-1"}, {"tvol": ""},
    {"tamt": "-1"}, {"tamt": "100 USD"}, {"zdiv": "-1"}, {"zdiv": "2.5"},
])
def test_normalizer_rejects_unknown_numeric_units_invalid_daily_dates_and_ohlc(changes):
    with pytest.raises(ValueError):
        normalized(bar(**changes))


@pytest.mark.parametrize("changes", [
    {"feed": "kis:krx"}, {"venue": "NYS"}, {"currency": "KRW"},
    {"quantity_unit": "lots"}, {"price_unit": "cents_per_share"}, {"session_scope": "intraday"},
])
def test_normalizer_requires_explicit_us_currency_venue_feed_and_share_units(changes):
    context = normalized().context
    with pytest.raises(ValueError):
        normalized(context=replace(context, **changes))


def test_normalizer_never_promotes_adjusted_basis_or_unverified_quantity_scope():
    for changes in ({"response_basis": "split_adjusted"}, {"quantity_scope": "intraday_cumulative"}):
        with pytest.raises(ValueError):
            normalized(**changes)


def test_normalizer_requires_capture_only_availability_and_ended_source_day():
    value = request()
    metadata = ObservationMetadata("p", "c", "raw", "rev", value.captured_at, value.captured_at)
    for changed in (
        replace(metadata, available_at=datetime(2024, 7, 9, tzinfo=timezone.utc)),
        replace(metadata, available_at=None),
        replace(metadata, available_at=datetime(2024, 7, 9, 18, tzinfo=timezone.utc),
                captured_at=datetime(2024, 7, 9, 18, tzinfo=timezone.utc)),
    ):
        with pytest.raises(ValueError):
            normalized(metadata=changed)


def test_normalizer_rejects_values_outside_supported_numeric_envelope_before_storage():
    with pytest.raises(ValueError, match="numeric_envelope"):
        normalized(bar(high="1e10001"))
