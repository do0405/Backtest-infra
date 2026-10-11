from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from backtest_data.prices.adapters.kis import KISDailyRequest, normalize_kis_bar
from backtest_data.prices.domain import PriceContext
from backtest_data.prices.normalize import ObservationMetadata


def normalize(**changes):
    # Archived Q530124 daily row: the supplier explicitly reports both zero
    # quantities with a positive, unchanged quote. No trading halt is asserted.
    row = {"stck_bsop_date": "20250113", "stck_oprc": "19955", "stck_hgpr": "19955",
           "stck_lwpr": "19955", "stck_clpr": "19955", "acml_vol": "0", "acml_tr_pbmn": "0"}
    captured = datetime(2026, 10, 9, tzinfo=timezone.utc)
    return normalize_kis_bar(row | changes,
        context=PriceContext("security", "security:KRX", "KRX", "kis:krx", "session_total", "KRW",
                             "currency_per_share", "shares", "as_traded"),
        metadata=ObservationMetadata("observation", "contract", "raw-supplier-report", "raw-hash", captured, captured),
        request=KISDailyRequest("Q530124", date(2016, 1, 1), date(2025, 2, 16)),
        response_basis="as_traded", quantity_scope="session_total")


def test_explicit_supplier_zero_quantities_and_positive_flat_quote_preserve_source_claim():
    result = normalize()
    assert result.volume == result.notional == Decimal(0)
    assert result.open == result.high == result.low == result.close == Decimal(19955)
    assert result.no_trade_evidence == "raw:raw-supplier-report#output2:20250113:acml_vol=0,acml_tr_pbmn=0"
    assert result.available_at == result.captured_at


@pytest.mark.parametrize("changes", [
    {"acml_tr_pbmn": ""}, {"acml_tr_pbmn": None}, {"acml_tr_pbmn": "1"},
    {"stck_oprc": "0", "stck_hgpr": "0", "stck_lwpr": "0", "stck_clpr": "0"},
    {"stck_oprc": "-1", "stck_hgpr": "-1", "stck_lwpr": "-1", "stck_clpr": "-1"},
    {"stck_oprc": "19950", "stck_lwpr": "19950"},
    {"stck_clpr": "19960"}, {"stck_clpr": ""}, {"stck_clpr": "NaN"},
    {"acml_vol": 0.0}, {"acml_tr_pbmn": False},
])
def test_unproven_or_inconsistent_zero_transaction_rows_stay_quarantined(changes):
    with pytest.raises(ValueError):
        normalize(**changes)


def test_missing_volume_and_amount_are_preserved_without_zero_transaction_claim():
    result = normalize(acml_vol="", acml_tr_pbmn="")
    assert result.volume is None and result.notional is None
    assert result.no_trade_evidence is None


def test_unknown_volume_with_reported_zero_amount_does_not_become_zero_volume():
    result = normalize(acml_vol="")
    assert result.volume is None and result.notional == Decimal(0)
    assert result.no_trade_evidence is None
