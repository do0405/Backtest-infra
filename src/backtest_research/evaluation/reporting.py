"""Read-only, reproducible post-run reports from exact saved inputs."""
from collections import deque
from datetime import date
from decimal import Context, Decimal, localcontext
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re

from backtest_research.contracts import canonical_hash
from backtest_research.results import verify_result
from . import calculate_metrics

REPORT_POLICY = 'research_post_run_analysis_v1'


def _decimal(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('finite ledger number required')
    return number


def _metric(value=None, reason=None):
    return dict(value=value, reason=reason)


def analyze_trades(ledger):
    """FIFO closed portions, with original fill references and residual open cost.

    Execution prices already include modeled slippage. It must not be deducted
    a second time. A partial exit is a portion, not a completed position episode.
    """
    with localcontext(Context(prec=28)):
        lots, quantities, closed = {}, {}, []
        previous = None
        for index, trade in enumerate(ledger):
            day = date.fromisoformat(trade['session'])
            if previous is not None and day < previous:
                raise ValueError('ordered ledger sessions required')
            previous = day
            quantity = trade['quantity']
            if type(quantity) is not int or quantity == 0:
                raise ValueError('nonzero integer ledger quantity required')
            price = _decimal(trade['price'])
            fee, tax = (_decimal(trade.get(key, 0)) for key in ('fee', 'tax'))
            if price <= 0 or min(fee, tax) < 0:
                raise ValueError('positive price and nonnegative ledger costs required')
            key = (trade['security_id'], trade.get('raw_series_key', ''))
            queue = lots.setdefault(key, deque())
            quantities.setdefault(key, 0)
            reference = dict(ledger_index=index, sequence=trade.get('sequence'), session=trade['session'])
            if quantity > 0:
                queue.append(dict(quantity=quantity, price=price, fee=fee, tax=tax, reference=reference))
                quantities[key] += quantity
                continue
            remaining, exit_fee, exit_tax = -quantity, fee, tax
            if quantities[key] < remaining:
                raise ValueError('unmatched sale without an earlier entry')
            quantities[key] -= remaining
            while remaining:
                lot = queue[0]
                count = min(remaining, lot['quantity'])
                entry_fee = lot['fee'] if count == lot['quantity'] else lot['fee'] * count / lot['quantity']
                entry_tax = lot['tax'] if count == lot['quantity'] else lot['tax'] * count / lot['quantity']
                allocated_exit_fee = exit_fee if count == remaining else exit_fee * count / remaining
                allocated_exit_tax = exit_tax if count == remaining else exit_tax * count / remaining
                gross = (price - lot['price']) * count
                costs = entry_fee + entry_tax + allocated_exit_fee + allocated_exit_tax
                net = gross - costs
                basis = lot['price'] * count + entry_fee + entry_tax
                closed.append(dict(security_id=key[0], raw_series_key=key[1], quantity=count,
                    entry_session=lot['reference']['session'], exit_session=trade['session'],
                    entry_sequence=lot['reference']['sequence'], exit_sequence=reference['sequence'],
                    entry_ledger_index=lot['reference']['ledger_index'], exit_ledger_index=index,
                    entry_price=str(lot['price']), exit_price=str(price), entry_fee=str(entry_fee),
                    exit_fee=str(allocated_exit_fee), entry_tax=str(entry_tax), exit_tax=str(allocated_exit_tax),
                    gross_pnl=str(gross), net_pnl=str(net), net_return=str(net / basis),
                    holding_calendar_days=(day - date.fromisoformat(lot['reference']['session'])).days))
                lot['quantity'] -= count
                lot['fee'] -= entry_fee
                lot['tax'] -= entry_tax
                remaining -= count
                exit_fee -= allocated_exit_fee
                exit_tax -= allocated_exit_tax
                if not lot['quantity']:
                    queue.popleft()
        outcomes = [_decimal(row['net_pnl']) for row in closed]
        gains = sum((value for value in outcomes if value > 0), Decimal(0))
        losses = -sum((value for value in outcomes if value < 0), Decimal(0))
        open_lots = [dict(security_id=key[0], raw_series_key=key[1], quantity=lot['quantity'],
                         entry_session=lot['reference']['session'], entry_sequence=lot['reference']['sequence'],
                         entry_ledger_index=lot['reference']['ledger_index'], entry_price=str(lot['price']),
                         remaining_entry_cost=str(lot['fee'] + lot['tax']),
                         cost_basis=str(lot['price'] * lot['quantity'] + lot['fee'] + lot['tax']))
                     for key, queue in sorted(lots.items()) for lot in queue]
        summary = dict(closed_portion_count=len(closed), closed_quantity=sum(row['quantity'] for row in closed),
                       winning_portions=sum(value > 0 for value in outcomes),
                       losing_portions=sum(value < 0 for value in outcomes),
                       breakeven_portions=sum(value == 0 for value in outcomes), open_quantity=sum(row['quantity'] for row in open_lots),
                       realized_gains=str(gains), realized_losses=str(losses), realized_net_pnl=str(sum(outcomes, Decimal(0))),
                       win_rate=_metric(sum(value > 0 for value in outcomes) / len(outcomes)) if outcomes else _metric(reason='no_closed_portions'),
                       profit_factor=_metric(float(gains / losses)) if losses else _metric(reason='no_realized_losses' if outcomes else 'no_closed_portions'),
                       average_net_pnl=str(sum(outcomes, Decimal(0)) / len(outcomes)) if outcomes else None)
        return dict(policy='fifo_closed_portions_v1', slippage='already_in_execution_prices_not_subtracted_again',
                    count_unit='closed_entry_lot_portion_not_position_episode', summary=summary,
                    closed_portions=closed, open_lots=open_lots)


def analyze_risk(nav_rows):
    """Additional descriptive risk measures; no out-of-sample certification."""
    with localcontext(Context(prec=28)):
        rows = list(nav_rows)
        metrics = calculate_metrics(rows, [])
        base_reason = metrics['total_return']['reason']
        names = ('sortino', 'calmar', 'max_drawdown_duration_calendar_days', 'mean_exposure', 'invested_session_fraction')
        report = dict(definitions=dict(sortino_target_per_session='0', downside_denominator='all_declared_session_returns',
            annualization_sessions=252, calmar='elapsed_day_cagr_divided_by_absolute_max_drawdown',
            drawdown_duration='calendar_days_from_last_equal_or_higher_peak_through_recovery_or_last_session',
            exposure_weighting='equal_declared_session_weight'))
        if base_reason:
            report.update({name: _metric(reason=base_reason) for name in names})
            return report
        values = [_decimal(row['nav']) for row in rows]
        returns = [float(b / a - 1) for a, b in zip(values, values[1:])]
        downside = math.sqrt(math.fsum(min(value, 0) ** 2 for value in returns) / len(returns)) if returns else 0
        report['sortino'] = _metric(math.fsum(returns) / len(returns) / downside * math.sqrt(252)) if downside else _metric(reason='no_downside_returns' if returns else 'insufficient_returns')
        drawdown = abs(metrics['max_drawdown']['value'])
        report['calmar'] = _metric(metrics['cagr']['value'] / drawdown) if drawdown and metrics['cagr']['value'] is not None else _metric(reason='zero_drawdown' if not drawdown else metrics['cagr']['reason'])
        peak, peak_day, duration, underwater = values[0], date.fromisoformat(rows[0]['session']), 0, False
        for row, value in zip(rows[1:], values[1:]):
            day = date.fromisoformat(row['session'])
            if underwater or value < peak:
                duration = max(duration, (day - peak_day).days)
            if value >= peak:
                peak, peak_day, underwater = value, day, False
            else:
                underwater = True
        report['max_drawdown_duration_calendar_days'] = _metric(duration)
        if any(row.get('exposure') is None for row in rows):
            report['mean_exposure'] = _metric(reason='exposure_unavailable')
            report['invested_session_fraction'] = _metric(reason='exposure_unavailable')
        else:
            exposures = [_decimal(row['exposure']) for row in rows]
            report['mean_exposure'] = _metric(str(sum(exposures, Decimal(0)) / len(exposures)))
            report['invested_session_fraction'] = _metric(sum(value > 0 for value in exposures) / len(exposures))
        return report


def build_analysis(result, dataset, *, benchmark_series_key=None):
    with localcontext(Context(prec=28)):
        return _build_analysis(result, dataset, benchmark_series_key=benchmark_series_key)


def _build_analysis(result, dataset, *, benchmark_series_key):
    verify_result(result)
    if result['dataset_id'] != dataset.dataset_id or result['fingerprint']['input_hash'] != dataset.content_hash:
        raise ValueError('analysis input hash mismatch')
    sessions = [day.isoformat() for day in dataset.sessions]
    if [row['session'] for row in result['rows']] != sessions:
        raise ValueError('analysis requires the exact saved input session grid')
    report = dict(policy=REPORT_POLICY, result_id=result['result_id'], dataset_id=dataset.dataset_id,
                  input_hash=dataset.content_hash, evaluation_scope='in_sample_research',
                  trades=analyze_trades(result['ledger']), risk=analyze_risk(result['rows']), benchmark=dict(status='not_selected', reason='benchmark_not_selected'),
                  comparison=dict(total_return_difference=_metric(reason='benchmark_not_selected')),
                  quality_gaps=result['quality_gaps'], currency=result['execution_profile']['currency'],
                  calendar_grade=dataset.calendar_grade)
    if benchmark_series_key is not None:
        identities = {series.key: series for series in dataset.series}
        if benchmark_series_key not in identities:
            raise ValueError('explicit benchmark must be a saved dataset series')
        identity = identities[benchmark_series_key]
        if (identity.market, identity.currency) != (result['execution_profile']['market'], result['execution_profile']['currency']):
            raise ValueError('benchmark must have the same market and currency')
        bars = {bar.session.isoformat(): bar for bar in dataset.bars if bar.series.key == benchmark_series_key}
        first = bars.get(sessions[0])
        with localcontext(Context(prec=28)):
            initial_nav = _decimal(result['rows'][0]['nav'])
            rows = [dict(session=day, nav=str(initial_nav * bars[day].close / first.close) if first and day in bars else None,
                         observation_id=bars[day].observation_id if day in bars else None,
                         input_hash=bars[day].input_hash if day in bars else None) for day in sessions]
        missing = [row['session'] for row in rows if row['nav'] is None]
        metrics = calculate_metrics(rows, []) if not missing else None
        report['benchmark'] = dict(status='incomplete' if missing else 'available', reason='incomplete_benchmark_sessions' if missing else None,
            series_key=benchmark_series_key, series_identity={field: getattr(identity, field) for field in identity.__dataclass_fields__},
            policy=dict(kind='normalized_close_price_zero_cost_v1', anchor_session=sessions[0], anchor_nav=str(initial_nav),
                        costs='zero', dividends='not_modeled', execution='none_price_comparison_not_buy_and_hold_execution',
                        missing_marks='null_no_carry', strategy_costs='retained_in_strategy_nav'),
            rows=rows, missing_sessions=missing, metrics=metrics)
        reason = 'incomplete_benchmark_sessions' if missing else 'stale_strategy_marks' if any(row.get('stale_mark_series_keys') for row in result['rows']) else None
        strategy_return = result['metrics']['total_return']
        if reason or strategy_return['value'] is None:
            difference = _metric(reason=reason or strategy_return['reason'])
        else:
            difference = _metric(strategy_return['value'] - metrics['total_return']['value'])
        report['comparison'] = dict(total_return_difference=difference, interpretation='strategy_net_price_return_minus_zero_cost_benchmark_price_return')
    report['code_hash'] = hashlib.sha256(Path(__file__).read_bytes() + Path(__file__).with_name('__init__.py').read_bytes()).hexdigest()
    report['analysis_id'] = canonical_hash(report)
    return report


def export_csv(rows, fields):
    """UTF-8 CSV; protect text formula cells while retaining signed numbers."""
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        cells = {}
        for field in fields:
            value = row.get(field)
            text = '' if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) if isinstance(value, (dict, list, tuple)) else str(value)
            stripped = text.lstrip()
            numeric = re.fullmatch(r'-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', text)
            if not numeric and (stripped.startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n'))):
                text = "'" + text
            cells[field] = text
        writer.writerow(cells)
    return output.getvalue()
