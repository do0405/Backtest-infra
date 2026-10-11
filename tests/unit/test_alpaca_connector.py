from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest_data.credentials import Credentials, Secret
from backtest_data.ingestion.http import ProviderError, ProviderHTTP, ProviderResponse
from backtest_data.prices.adapters.alpaca import AlpacaDailyRequest, normalize_alpaca_bar
from backtest_data.prices.domain import PriceContext
from backtest_data.prices.normalize import ObservationMetadata
import backtest_data.providers as providers


def request(**changes):
    return AlpacaDailyRequest(date(2024, 1, 1), date(2024, 7, 10), symbol="BRK.B",
        captured_at=datetime(2024, 7, 10, 18, tzinfo=timezone.utc), **changes)


def test_free_request_is_explicit_and_does_not_fetch_an_unclosed_new_york_day():
    value = request(page_token="opaque==")
    params = value.params()
    assert params["feed"] == "sip"
    assert params["adjustment"] == "raw"
    assert params["timeframe"] == "1Day"
    assert params["asof"] == "-"
    assert params["sort"] == "asc"
    assert params["page_token"] == "opaque=="
    assert datetime.fromisoformat(params["start"]).hour == 0
    assert datetime.fromisoformat(params["end"]) <= value.captured_at - timedelta(minutes=16)
    assert value.effective_end == date(2024, 7, 9)
    assert value.end == date(2024, 7, 10)
    assert value.page_size == 10000
    assert params["limit"] == "10000"


@pytest.mark.parametrize("page_size", [0, -1, 10001, True, False, 100.0, "100", None])
def test_page_size_requires_an_integer_within_alpaca_bounds(page_size):
    with pytest.raises(ValueError, match="page_size"):
        request(page_size=page_size)


@pytest.mark.parametrize("page_size", [1, 100, 10000])
def test_page_size_changes_only_limit_and_preserves_whole_requested_window(page_size):
    original = AlpacaDailyRequest(date(2016, 1, 1), date(2026, 10, 7), symbol="AAPL",
        captured_at=datetime(2026, 10, 10, 18, tzinfo=timezone.utc), page_token="opaque==")
    bounded = replace(original, page_size=page_size)

    assert bounded.params()["limit"] == str(page_size)
    assert bounded.params() | {"limit": "10000"} == original.params()
    assert bounded.start == date(2016, 1, 1)
    assert bounded.end == bounded.effective_end == date(2026, 10, 7)
    assert bounded.captured_at == original.captured_at
    assert bounded.page_token == "opaque=="


def test_cutoff_waits_for_full_new_york_day_to_be_sixteen_minutes_old():
    value = replace(request(), captured_at=datetime(2024, 7, 10, 4, 10, tzinfo=timezone.utc))
    assert value.effective_end == date(2024, 7, 8)
    value = replace(value, captured_at=datetime(2024, 7, 10, 4, 16, tzinfo=timezone.utc))
    assert value.effective_end == date(2024, 7, 9)


@pytest.mark.parametrize("symbol", ["../orders", "AAPL?feed=iex", "AAPL/quotes", "aapl", "", "AAPL\\x"])
def test_symbol_is_an_explicit_safe_uppercase_identity(symbol):
    with pytest.raises(ValueError, match="symbol"):
        replace(request(), symbol=symbol).endpoint()


def test_directory_preferred_symbol_punctuation_is_encoded_without_changing_identity():
    value = replace(request(), symbol="BAC$B")
    assert value.symbol == "BAC$B"
    assert value.endpoint() == "/v2/stocks/BAC%24B/bars"
    http = ProviderHTTP(None)
    http._validate("alpaca", "GET", "https://data.alpaca.markets" + value.endpoint())
    with pytest.raises(ValueError):
        http._validate("alpaca", "GET", "https://data.alpaca.markets/v2/stocks/AAPL%2Fquotes/bars")
    http.close()


def test_default_normalizer_uses_new_york_midnight_and_positive_prices():
    value = request()
    context = PriceContext("s", "s:SIP", "US_CONSOLIDATED", "sip", "new_york_calendar_day", "USD",
                           "currency_per_share", "shares", "as_traded")
    metadata = ObservationMetadata("p", "c", "raw", "rev", value.captured_at, value.captured_at)
    row = {"t": "2024-07-09T04:00:00Z", "o": "100", "h": "110", "l": "90", "c": "105", "v": 100, "vw": "102.125"}
    normalized = normalize_alpaca_bar(row, context=context, metadata=metadata, request=value, response_basis="as_traded")
    assert normalized.session == date(2024, 7, 9)
    assert normalized.notional is None
    assert normalized.provider_vwap == Decimal("102.125")
    for invalid in ({"t": "2024-07-09T00:00:00Z"}, {"o": "0", "l": "0"}, {"v": -1}, {"o": 100.1}, {"t": "2024-07-10T04:00:00Z"}):
        with pytest.raises(ValueError):
            normalize_alpaca_bar(row | invalid, context=context, metadata=metadata, request=value, response_basis="as_traded")


class CaptureHTTP:
    def __init__(self, content=b'{"symbol":"BRK.B","bars":[{"c":100.0100}],"next_page_token":null}'):
        self.content, self.requests = content, []

    def request(self, source, method, url, **kwargs):
        self.requests.append((source, method, url, kwargs))
        return ProviderResponse(self.content, 200, request().captured_at, "application/json")


def credentials(key="fake-key", secret="fake-secret"):
    return Credentials(Secret(""), Secret(""), Secret(""), Secret(key), Secret(secret), "")


def test_provider_confines_auth_to_headers_and_decodes_decimal_without_float():
    http = CaptureHTTP()
    client = providers.AlpacaData(http, credentials())
    response, payload = client.daily_prices(request())
    source, method, url, kwargs = http.requests[0]
    assert (source, method, url) == ("alpaca", "GET", "https://data.alpaca.markets/v2/stocks/BRK.B/bars")
    assert kwargs["headers"] == {"APCA-API-KEY-ID": "fake-key", "APCA-API-SECRET-KEY": "fake-secret"}
    assert "fake" not in str(kwargs["params"])
    assert payload["bars"][0]["c"] == Decimal("100.0100")
    assert isinstance(payload["bars"][0]["c"], Decimal)
    assert response is not None


def test_provider_requires_keys_and_never_requests_recent_or_empty_bounded_range():
    http = CaptureHTTP()
    with pytest.raises(ProviderError, match="requires_credentials"):
        providers.AlpacaData(http, credentials(key="")).daily_prices(request())
    value = replace(request(), start=date(2024, 7, 10))
    with pytest.raises(ProviderError, match="free_history_range_deferred"):
        providers.AlpacaData(http, credentials()).daily_prices(value)
    assert http.requests == []


@pytest.mark.parametrize("content", [b'[]', b'{"bars":NaN}', b'{broken'])
def test_provider_rejects_invalid_json_without_echoing_response(content):
    with pytest.raises(ProviderError, match="invalid_json"):
        providers.AlpacaData(CaptureHTTP(content), credentials()).daily_prices(request())


def test_transport_only_allows_registered_alpaca_daily_data_reads():
    http = ProviderHTTP(None)
    http._validate("alpaca", "GET", "https://data.alpaca.markets/v2/stocks/AAPL/bars")
    assert http.intervals["alpaca"] >= 0.3
    for method, url in [("POST", "https://data.alpaca.markets/v2/stocks/AAPL/bars"),
                        ("GET", "https://api.alpaca.markets/v2/orders"),
                        ("GET", "https://data.alpaca.markets/v2/stocks/AAPL/trades/latest"),
                        ("GET", "https://data.alpaca.markets:444/v2/stocks/AAPL/bars")]:
        with pytest.raises(ValueError):
            http._validate("alpaca", method, url)
    http.close()
