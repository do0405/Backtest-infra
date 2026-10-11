from datetime import date
import pytest
from backtest_research.evaluation import calculate_metrics


def test_elapsed_day_cagr_drawdown_and_cost_turnover():
    nav = [{'session': '2020-01-01', 'nav': '100'}, {'session': '2020-07-01', 'nav': '80'},
           {'session': '2021-01-01', 'nav': '110'}]
    ledger = [{'session': '2020-07-01', 'quantity': 2, 'price': '10', 'fee': '1', 'slippage_cost': '.5', 'tax': '0'}]
    result = calculate_metrics(nav, ledger)
    assert result['total_return']['value'] == pytest.approx(.1)
    assert result['cagr']['value'] == pytest.approx(1.1 ** (365.2425 / 366) - 1)
    assert result['max_drawdown']['value'] == pytest.approx(-.2)
    assert result['turnover']['value'] == pytest.approx(.2)
    assert result['costs'] == {'fees': '1', 'slippage': '0.5', 'tax': '0'}
    assert result['evaluation_scope'] == 'in_sample_research'


def test_no_denominator_and_constant_returns_are_explicit():
    assert calculate_metrics([], [])['cagr']['reason'] == 'empty_nav'
    result = calculate_metrics([{'session': '2020-01-01', 'nav': '0'}], [])
    assert result['cagr']['value'] is None
    assert result['cagr']['reason'] == 'nonpositive_nav'
    flat = calculate_metrics([{'session': '2020-01-01', 'nav': '100'}, {'session': '2020-01-02', 'nav': '100'}], [])
    assert flat['sharpe']['reason'] == 'zero_return_variance'
    assert flat['trade_win_rate']['reason'] == 'no_closed_lots'
