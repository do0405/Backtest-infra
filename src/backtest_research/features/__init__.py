"""Causal price features over the dataset's declared session grid."""
from decimal import Context, Decimal, localcontext
import math
from statistics import stdev
from backtest_research.contracts import canonical_hash, canonical_value


FEATURE_VERSION = 'declared_price_features_v1'


def compute_price_features(dataset, series_key, *, sma_period=20, rsi_period=2,
                           volatility_period=20, momentum_lookback=252, momentum_skip=21):
    with localcontext(Context(prec=28)):
        return _compute_price_features(dataset, series_key, sma_period=sma_period, rsi_period=rsi_period,
                                       volatility_period=volatility_period, momentum_lookback=momentum_lookback,
                                       momentum_skip=momentum_skip)


def _compute_price_features(dataset, series_key, *, sma_period, rsi_period,
                            volatility_period, momentum_lookback, momentum_skip):
    periods = (sma_period, rsi_period, volatility_period, momentum_lookback, momentum_skip)
    if any(type(n) is not int or n < 1 or n > 6000 for n in periods) or volatility_period < 2 or momentum_skip >= momentum_lookback:
        raise ValueError('invalid feature period')
    bars = {bar.session: bar for bar in dataset.series_bars(series_key)}
    if not bars:
        raise ValueError('unknown or empty series')
    series = next(iter(bars.values())).series
    semantics = ('raw_as_traded' if series.basis == 'as_traded' else
                 'split_adjusted_price_excluding_dividends' if series.adjustments == ('split',) else
                 'provider_adjusted_declared')
    definitions = dict(version=FEATURE_VERSION, basis=series.basis, adjustments=list(series.adjustments),
                       price_semantics=semantics, sma_period=sma_period, rsi_period=rsi_period,
                       volatility_period=volatility_period, momentum_lookback=momentum_lookback,
                       momentum_skip=momentum_skip, annualization_sessions=252, volatility_ddof=1,
                       rsi_method='wilder_initial_mean_then_alpha_1_over_n', missing_policy='declared_grid_no_compression',
                       numeric_policy='decimal_context28_price_arithmetic_log_std_float64_v1')
    prices = [bars[session].close if session in bars else None for session in dataset.sessions]
    rows, gain, loss, changes, previous = [], None, None, [], None
    for i, session in enumerate(dataset.sessions):
        price = prices[i]
        row = dict(session=session.isoformat(), price=str(price) if price is not None else None,
                   simple_return=None, sma=None, rsi=None, volatility=None, momentum=None, statuses={})
        row['statuses']['price'] = {'status': 'available' if price is not None else 'unavailable',
                                    'reason': None if price is not None else 'missing_session'}
        row['input_refs'] = ([{'observation_id': bars[session].observation_id, 'input_hash': bars[session].input_hash,
                               'raw_id': bars[session].raw_id, 'decision_id': bars[session].decision_id}]
                             if session in bars else [])
        if price is None:
            gain, loss, changes, previous = None, None, [], None
        elif previous is not None:
            delta = price - previous
            up, down = max(delta, Decimal(0)), max(-delta, Decimal(0))
            changes.append((up, down))
            if gain is None and len(changes) == rsi_period:
                gain = sum(item[0] for item in changes) / rsi_period
                loss = sum(item[1] for item in changes) / rsi_period
            elif gain is not None:
                gain = (gain * (rsi_period - 1) + up) / rsi_period
                loss = (loss * (rsi_period - 1) + down) / rsi_period
            if gain is not None:
                row['rsi'] = str(Decimal(50) if gain == loss == 0 else
                                 Decimal(100) if loss == 0 else Decimal(100) * gain / (gain + loss))
        previous = price

        requirements = {'simple_return': 2, 'sma': sma_period, 'rsi': rsi_period + 1,
                        'volatility': volatility_period + 1, 'momentum': momentum_lookback + 1}
        for name, size in requirements.items():
            window = prices[max(0, i - size + 1):i + 1]
            reason = 'missing_session' if any(p is None for p in window) else 'warmup' if len(window) < size else None
            row['statuses'][name] = {'status': 'unavailable' if reason else 'available', 'reason': reason}
            if reason:
                continue
            if name == 'simple_return':
                row[name] = str(price / prices[i - 1] - 1)
            elif name == 'sma':
                row[name] = str(sum(window) / sma_period)
            elif name == 'volatility':
                logs = [math.log(float(b / a)) for a, b in zip(window, window[1:])]
                row[name] = stdev(logs) * math.sqrt(252)
            elif name == 'momentum':
                row[name] = str(prices[i - momentum_skip] / prices[i - momentum_lookback] - 1)
        rows.append(row)
    return dict(dataset_id=dataset.dataset_id, series_key=series_key, series=canonical_value(series),
                definition_hash=canonical_hash(definitions), definitions=definitions, rows=rows,
                quality_gaps=list(dataset.quality_gaps), original_target_count=dataset.original_target_count)
