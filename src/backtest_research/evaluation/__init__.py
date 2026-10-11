"""Pure performance evaluation; no strategy selection or certification."""
from datetime import date
from decimal import Decimal
import math
from statistics import mean, stdev


def _metric(value=None, reason=None):
    return {'value': value, 'reason': reason}


def calculate_metrics(nav_rows, ledger):
    nav_rows, ledger = list(nav_rows), list(ledger)
    costs = {'fees': Decimal(0), 'slippage': Decimal(0), 'tax': Decimal(0)}
    for trade in ledger:
        for result_key, trade_key in [('fees', 'fee'), ('slippage', 'slippage_cost'), ('tax', 'tax')]:
            costs[result_key] += Decimal(str(trade.get(trade_key, 0)))
    result = dict(evaluation_scope='in_sample_research', annualization_sessions=252, risk_free_rate=0,
                  costs={k: str(v) for k, v in costs.items()}, total_return=_metric(), cagr=_metric(),
                  max_drawdown=_metric(), annualized_volatility=_metric(), sharpe=_metric(), turnover=_metric(),
                  trade_win_rate=_metric(reason='no_closed_lots'))
    names = ('total_return', 'cagr', 'max_drawdown', 'annualized_volatility', 'sharpe', 'turnover')
    if not nav_rows:
        result.update({name: _metric(reason='empty_nav') for name in names})
        return result
    values = [Decimal(str(row['nav'])) for row in nav_rows]
    if any(not value.is_finite() or value <= 0 for value in values):
        result.update({name: _metric(reason='nonpositive_nav') for name in names})
        return result
    dates = [date.fromisoformat(str(row['session'])) for row in nav_rows]
    if dates != sorted(set(dates)):
        raise ValueError('ordered unique NAV sessions required')
    result['total_return'] = _metric(float(values[-1] / values[0] - 1))
    elapsed = (dates[-1] - dates[0]).days
    result['cagr'] = _metric(float(values[-1] / values[0]) ** (365.2425 / elapsed) - 1) if elapsed else _metric(reason='zero_elapsed_days')
    high, drawdown = values[0], Decimal(0)
    for value in values:
        high = max(high, value)
        drawdown = min(drawdown, value / high - 1)
    result['max_drawdown'] = _metric(float(drawdown))
    returns = [float(b / a - 1) for a, b in zip(values, values[1:])]
    std = stdev(returns) if len(returns) > 1 else 0
    result['annualized_volatility'] = _metric(std * math.sqrt(252)) if returns else _metric(reason='insufficient_returns')
    result['sharpe'] = _metric(mean(returns) / std * math.sqrt(252)) if std else _metric(reason='zero_return_variance' if returns else 'insufficient_returns')
    prior = {dates[i].isoformat(): values[i - 1] for i in range(1, len(dates))}
    turnover, missing = Decimal(0), False
    for trade in ledger:
        denominator = prior.get(str(trade['session']))
        if denominator is None:
            missing = True
            continue
        turnover += abs(Decimal(str(trade['quantity'])) * Decimal(str(trade['price']))) / denominator
    result['turnover'] = _metric(reason='missing_previous_nav') if missing else _metric(float(turnover))
    # FIFO closes include entry and exit commission; open lots do not enter win rate.
    lots, outcomes = {}, []
    for trade in ledger:
        key = trade.get('security_id', 'default')
        quantity, price = int(trade['quantity']), Decimal(str(trade['price']))
        fee = Decimal(str(trade.get('fee', 0))) + Decimal(str(trade.get('tax', 0)))
        if quantity > 0:
            lots.setdefault(key, []).append([quantity, price + fee / quantity])
        elif quantity < 0:
            remaining, net = -quantity, price - fee / (-quantity)
            while remaining and lots.get(key):
                lot = lots[key][0]
                count = min(remaining, lot[0])
                outcomes.append((net - lot[1]) * count)
                lot[0] -= count
                remaining -= count
                if not lot[0]:
                    lots[key].pop(0)
    if outcomes:
        result['trade_win_rate'] = _metric(sum(value > 0 for value in outcomes) / len(outcomes))
    return result
