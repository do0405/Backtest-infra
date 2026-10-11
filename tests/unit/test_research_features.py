import math
from decimal import Decimal
import pytest
from backtest_research.features import compute_price_features
from tests.oracles.research_ledger import dataset


def features(prices, **kwargs):
    data = dataset(prices, **kwargs)
    return compute_price_features(data, data.series[0].key, sma_period=2, rsi_period=2,
                                  volatility_period=2, momentum_lookback=3, momentum_skip=1)


def test_hand_sma_wilder_rsi_returns_volatility_and_momentum():
    rows = features([10, 12, 11, 13, 12])['rows']
    assert Decimal(rows[1]['sma']) == 11
    assert Decimal(rows[2]['rsi']) == Decimal(100) * Decimal(1) / Decimal('1.5')
    assert Decimal(rows[3]['rsi']) == Decimal(100) * Decimal('1.5') / Decimal('1.75')
    assert Decimal(rows[1]['simple_return']) == Decimal('.2')
    expected = abs(math.log(12 / 10) - math.log(11 / 12)) / math.sqrt(2) * math.sqrt(252)
    assert rows[2]['volatility'] == pytest.approx(expected)
    assert Decimal(rows[3]['momentum']) == Decimal('.1')
    assert rows[0]['statuses']['sma']['reason'] == 'warmup'


def test_flat_and_increasing_rsi_edges():
    assert Decimal(features([10, 10, 10])['rows'][2]['rsi']) == 50
    assert Decimal(features([10, 11, 12])['rows'][2]['rsi']) == 100


def test_missing_sessions_do_not_compress_windows():
    rows = features([10, 11, 12, 13, 14, 15], missing=(2,))['rows']
    assert rows[2]['price'] is None
    assert rows[3]['sma'] is None
    assert rows[3]['statuses']['sma']['reason'] == 'missing_session'
    assert rows[4]['rsi'] is None
    assert rows[5]['rsi'] is not None
    assert rows[5]['momentum'] is None


def test_prefix_invariance_and_declared_raw_definition():
    short, long = features([10, 12, 11]), features([10, 12, 11, 13, 12])
    assert short['rows'] == long['rows'][:3]
    assert short['definitions']['basis'] == 'as_traded'
    assert short['definitions']['adjustments'] == []
    adjusted = features([10, 12, 11], basis='provider_adjusted', adjustments=('dividend',))
    assert adjusted['definitions']['price_semantics'] == 'provider_adjusted_declared'


def test_period_validation_and_unknown_series():
    data = dataset([10, 11, 12])
    with pytest.raises(ValueError, match='series'):
        compute_price_features(data, 'unknown')
    with pytest.raises(ValueError, match='period'):
        compute_price_features(data, data.series[0].key, sma_period=0)


def test_decimal_context_does_not_change_declared_feature_definition():
    from decimal import localcontext
    expected = features([10, 12, 11, 13])
    with localcontext() as context:
        context.prec = 8
        actual = features([10, 12, 11, 13])
    assert actual == expected
