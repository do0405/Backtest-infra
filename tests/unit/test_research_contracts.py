from datetime import date, datetime, timezone
from decimal import Decimal
import pytest
from backtest_research.contracts import Bar, SeriesIdentity, AnalysisDataset, canonical_hash


def identity(**changes):
    values = dict(security_id='security', source_contract_id='contract', market='US', source='alpaca',
                  currency='USD', venue='XNYS', feed='sip', trade_scope='provider_daily', basis='as_traded',
                  adjustments=(), volume_basis='as_traded', quantity_unit='shares')
    return SeriesIdentity(**(values | changes))


def bar(**changes):
    values = dict(observation_id='bar', series=identity(), session=date(2020, 1, 2),
                  open=Decimal('10'), high=Decimal('12'), low=Decimal('9'), close=Decimal('11'),
                  volume=Decimal('100'), captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                  available_at=datetime(2020, 1, 3, tzinfo=timezone.utc), input_hash='a' * 64)
    return Bar(**(values | changes))


def test_adjusted_does_not_implicitly_mean_split():
    with pytest.raises(ValueError, match='adjustment'):
        identity(basis='provider_adjusted')
    assert identity(basis='provider_adjusted', adjustments=('dividend',)).adjustments != ('split',)


def test_invalid_bar_rejected_and_exact_decimal_preserved():
    with pytest.raises(ValueError, match='finite'):
        bar(close=Decimal('NaN'))
    with pytest.raises(ValueError, match='OHLC'):
        bar(high=Decimal('8'))
    assert bar(close=Decimal('11.0000000000000000001')).close == Decimal('11.0000000000000000001')


def test_dataset_retains_full_target_and_rejects_future_bar():
    b = bar()
    d = AnalysisDataset.create(bars=(b,), sessions=(b.session,), cutoff=datetime(2020, 1, 4, tzinfo=timezone.utc),
        manifest_id='manifest', manifest_hash='b' * 64, original_target_count=17758,
        source_snapshot_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        quality_gaps=('historical_universe_unverified',), origin='actual_source')
    assert d.original_target_count == 17758
    assert d.selected_security_ids == ('security',)
    with pytest.raises(ValueError, match='cutoff'):
        AnalysisDataset.create(bars=(b,), sessions=(b.session,), cutoff=datetime(2020, 1, 2, tzinfo=timezone.utc),
            manifest_id='manifest', manifest_hash='b' * 64, original_target_count=17758,
            source_snapshot_at=datetime(2026, 1, 1, tzinfo=timezone.utc), quality_gaps=(), origin='actual_source')


def test_hash_exact_numbers_order_and_reproducibility():
    assert canonical_hash({'v': Decimal('1.1'), 'x': 2}) == canonical_hash({'x': 2, 'v': Decimal('1.1')})
    assert canonical_hash({'v': Decimal('1.0000000000000000001')}) != canonical_hash({'v': Decimal('1')})
