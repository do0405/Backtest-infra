from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest

from backtest_data.prices.domain import PriceContext, PriceObservation
from backtest_data.prices.normalize import decimal_value, normalize_observation
from backtest_data.prices.validation import validate_observation


NOW = datetime(2024, 1, 5, tzinfo=timezone.utc)


def context(**changes):
    return replace(PriceContext("s1", "l1", "XNYS", "sip", "regular", "USD", "currency_per_share", "shares", "as_traded"), **changes)


def observation(**changes):
    return replace(PriceObservation("p1", context(), date(2024, 1, 4), D("100"), D("110"), D("90"), D("105"), D("100"), None, "c1", "hash1", "r1", NOW, NOW), **changes)


def test_missing_notional_is_not_zero_and_decimal_is_explicit():
    value = normalize_observation(observation())
    assert value.notional is None
    assert decimal_value("100.0100") == D("100.0100")
    with pytest.raises(ValueError, match="float"):
        decimal_value(100.1)


@pytest.mark.parametrize("changes,code", [
    ({"high": D("99")}, "invalid_ohlc"),
    ({"close": D("NaN")}, "nonfinite"),
    ({"volume": D("-1")}, "negative_quantity"),
    ({"notional": D("-1")}, "negative_notional"),
    ({"volume": D("0")}, "zero_without_evidence"),
    ({"complete": False}, "incomplete_session"),
])
def test_rejects_invalid_observations(changes, code):
    issues = validate_observation(observation(**changes))
    assert code in {i.code for i in issues}
    with pytest.raises(ValueError):
        normalize_observation(observation(**changes))


def test_no_trade_zero_is_explicit_but_missing_volume_is_retained():
    assert normalize_observation(observation(volume=D("0"), no_trade_evidence="provider:no executions")).volume == 0
    assert normalize_observation(observation(volume=None)).volume is None


def test_adjusted_input_cannot_be_normalized_as_raw_or_units_guessed():
    with pytest.raises(ValueError, match="basis"):
        normalize_observation(observation(context=context(basis="split_adjusted")), requested_basis="as_traded")
    with pytest.raises(ValueError, match="quantity_unit"):
        normalize_observation(observation(context=context(quantity_unit="")))


def test_requested_end_and_timezone_are_checked():
    with pytest.raises(ValueError, match="requested"):
        normalize_observation(observation(), requested_end=date(2024, 1, 3))
    with pytest.raises(ValueError, match="timezone"):
        observation(available_at=datetime(2024, 1, 5))
