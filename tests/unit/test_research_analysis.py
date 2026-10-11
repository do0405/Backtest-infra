from copy import deepcopy
from decimal import Decimal, ROUND_DOWN, localcontext
import csv
import io

import pytest

from backtest_research.evaluation.reporting import analyze_risk, analyze_trades, build_analysis, export_csv
from backtest_research.engines import run_backtest
from backtest_research.results import seal_result
from tests.oracles.research_ledger import dataset


def trade(day, quantity, price, fee='0', tax='0', security='A', sequence=1):
    return dict(session=day, signal_session=day, security_id=security, raw_series_key='r',
                quantity=quantity, price=price, fee=fee, tax=tax, slippage_cost='9', sequence=sequence)


def test_fifo_partial_exits_allocate_costs_keep_open_lots_and_do_not_double_count_slippage():
    ledger = [trade('2020-01-02', 10, '10', fee='2'),
              trade('2020-01-03', 5, '20', fee='1', sequence=2),
              trade('2020-01-06', -12, '15', fee='3', tax='1.2', sequence=3)]
    report = analyze_trades(ledger)
    first, second = report['closed_portions']
    assert first['quantity'] == 10 and second['quantity'] == 2
    assert first['gross_pnl'] == '50' and first['net_pnl'] == '44.5'
    assert second['gross_pnl'] == '-10' and Decimal(second['net_pnl']) == Decimal('-11.10')
    assert first['entry_sequence'] == 1 and first['exit_sequence'] == 3
    assert Decimal(report['summary']['realized_net_pnl']) == Decimal('33.40')
    assert report['summary']['win_rate']['value'] == .5
    assert report['summary']['profit_factor']['value'] == pytest.approx(44.5 / 11.1)
    assert report['open_lots'][0]['quantity'] == 3
    assert report['open_lots'][0]['remaining_entry_cost'] == '0.6'
    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_DOWN
        assert analyze_trades(ledger) == report


def test_trade_summary_no_closed_lots_no_losses_and_unmatched_sale_are_explicit():
    assert analyze_trades([])['summary']['win_rate']['reason'] == 'no_closed_portions'
    winning = analyze_trades([trade('2020-01-02', 1, '10'), trade('2020-01-03', -1, '11', sequence=2)])
    assert winning['summary']['profit_factor']['reason'] == 'no_realized_losses'
    with pytest.raises(ValueError, match='unmatched'):
        analyze_trades([trade('2020-01-03', -1, '11')])
    with pytest.raises(ValueError, match='ordered'):
        analyze_trades([trade('2020-01-03', 1, '11'), trade('2020-01-02', 1, '10')])


def result(data):
    return run_backtest(data, raw_series_keys=[data.series[0].key], initial_cash='100',
                        parameters={'fast_period': 1, 'slow_period': 2})


def test_explicit_benchmark_normalizes_same_close_grid_and_binds_original_input():
    data = dataset([10, 12, 16, 8, 8], opens=[10, 12, 15, 8, 8])
    original = result(data)
    preserved = deepcopy(original)
    report = build_analysis(original, data, benchmark_series_key=data.series[0].key)
    benchmark = report['benchmark']
    assert [Decimal(row['nav']) for row in benchmark['rows']] == [Decimal(value) for value in ['100', '120', '160', '80', '80']]
    assert benchmark['policy']['kind'] == 'normalized_close_price_zero_cost_v1'
    assert benchmark['metrics']['total_return']['value'] == pytest.approx(-.2)
    assert report['comparison']['total_return_difference']['value'] == pytest.approx(-.22)
    assert benchmark['rows'][0]['observation_id'] == '0'
    assert benchmark['series_identity']['basis'] == 'as_traded'
    assert report['result_id'] == original['result_id'] and report['dataset_id'] == data.dataset_id
    assert build_analysis(original, data, benchmark_series_key=data.series[0].key) == report
    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_DOWN
        assert build_analysis(original, data, benchmark_series_key=data.series[0].key) == report
    assert original == preserved
    assert build_analysis(original, data)['benchmark']['reason'] == 'benchmark_not_selected'


def test_benchmark_gap_is_not_carried_or_compressed_and_stale_comparison_is_unavailable():
    data = dataset([10, 12, 16, 17, 18], missing=(3,))
    report = build_analysis(result(data), data, benchmark_series_key=data.series[0].key)
    assert len(report['benchmark']['rows']) == 5
    assert report['benchmark']['rows'][3]['nav'] is None
    assert report['benchmark']['status'] == 'incomplete'
    assert report['comparison']['total_return_difference']['reason'] == 'incomplete_benchmark_sessions'
    assert report['benchmark']['metrics'] is None
    with pytest.raises(ValueError, match='input'):
        build_analysis(result(data), dataset([10, 11, 12]))
    with pytest.raises(ValueError, match='explicit'):
        build_analysis(result(data), data, benchmark_series_key='a' * 64)


def test_incompatible_benchmark_rejected_and_csv_preserves_numbers_safely():
    data = dataset([10, 11, 12])
    altered = result(data)
    altered['execution_profile']['currency'] = 'KRW'
    altered = seal_result(altered)
    with pytest.raises(ValueError, match='market and currency'):
        build_analysis(altered, data, benchmark_series_key=data.series[0].key)
    parsed = list(csv.DictReader(io.StringIO(export_csv([{'security_id': '=WEBSERVICE("x")', 'net_pnl': '-11.10'}], ['security_id', 'net_pnl']))))
    assert parsed == [{'security_id': "'=WEBSERVICE(\"x\")", 'net_pnl': '-11.10'}]


def test_repeated_partial_exits_conserve_all_entry_costs_and_stale_marks_block_comparison():
    ledger = [trade('2020-01-02', 3, '10', fee='1')] + [trade(f'2020-01-0{day}', -1, '11', sequence=day) for day in (3, 6, 7)]
    report = analyze_trades(ledger)
    assert sum(Decimal(row['entry_fee']) for row in report['closed_portions']) == Decimal('1')
    assert Decimal(report['summary']['realized_net_pnl']) == Decimal('2')
    data = dataset([10, 11, 12])
    saved = result(data)
    saved['rows'][1]['stale_mark_series_keys'] = [data.series[0].key]
    saved = seal_result(saved)
    assert build_analysis(saved, data, benchmark_series_key=data.series[0].key)['comparison']['total_return_difference']['reason'] == 'stale_strategy_marks'


def test_risk_summary_hand_values_zero_target_sortino_calmar_exposure_and_underwater_days():
    import math
    rows = [{'session': '2020-01-01', 'nav': '100', 'exposure': '0'},
            {'session': '2020-07-01', 'nav': '80', 'exposure': '.5'},
            {'session': '2021-01-01', 'nav': '110', 'exposure': '1'}]
    risk = analyze_risk(rows)
    assert risk['sortino']['value'] == pytest.approx((( -.2 + .375) / 2) / math.sqrt(.04 / 2) * math.sqrt(252))
    assert risk['calmar']['value'] == pytest.approx((1.1 ** (365.2425 / 366) - 1) / .2)
    assert risk['max_drawdown_duration_calendar_days']['value'] == 366
    assert risk['mean_exposure']['value'] == '0.5'
    assert risk['invested_session_fraction']['value'] == pytest.approx(2 / 3)
    flat = analyze_risk([{'session': '2020-01-01', 'nav': '100'}, {'session': '2020-01-02', 'nav': '100'}])
    assert flat['sortino']['reason'] == 'no_downside_returns'
    assert flat['calmar']['reason'] == 'zero_drawdown'
    assert flat['mean_exposure']['reason'] == 'exposure_unavailable'
