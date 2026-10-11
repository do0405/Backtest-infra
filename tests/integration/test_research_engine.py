"""Framework qualification is offline and does not require PostgreSQL."""
from dataclasses import replace
from decimal import Decimal
import pytest
from backtest_research.engines import run_backtest, UnsupportedResearchInput
from backtest_research.results import replay_result
from tests.oracles.research_ledger import dataset, hand_next_open_ledger


def run(data, **options):
    return run_backtest(data, raw_series_keys=[s.key for s in data.series if s.basis == 'as_traded'],
                        initial_cash='100', engine='backtrader', parameters={'sma_period': 2} | options.pop('parameters', {}), **options)


def test_actual_framework_next_open_ledger_and_replay():
    data = dataset([10, 12, 16, 8, 8], opens=[10, 12, 15, 8, 8])
    result = run(data)
    oracle = hand_next_open_ledger()
    assert result['engine']['name'] in ('backtrader', 'zipline-reloaded')
    assert result['engine']['actual_framework_execution'] is True
    assert [int(row['quantity']) for row in result['ledger']] == [oracle['buy_quantity'], oracle['sell_quantity']]
    assert [float(row['price']) for row in result['ledger']] == [oracle['buy_price'], oracle['sell_price']]
    assert [float(row['nav']) for row in result['rows']] == oracle['nav']
    assert float(result['rows'][-1]['cash']) == oracle['final_cash']
    assert result['evaluation_scope'] == 'in_sample_research'
    assert result['execution_profile']['tradability_observed'] == 'unknown'
    assert result['execution_profile']['assumed_trade_count'] == 2
    assert replay_result(result, data)['result_id'] == result['result_id']


def test_costs_slippage_integer_sizing_and_shortage_are_framework_ledger_values():
    result = run(dataset([10, 12, 16, 8, 8], opens=[10, 12, 15, 8, 8]), fee_bps='100', slippage_bps='100')
    buy, sell = result['ledger']
    assert buy['quantity'] == 6
    assert float(buy['price']) == pytest.approx(15.15)
    assert float(buy['fee']) == pytest.approx(.909)
    assert float(sell['price']) == pytest.approx(7.92)
    assert float(sell['fee']) == pytest.approx(.4752)
    assert float(result['rows'][-1]['cash']) == pytest.approx(55.2358)
    assert float(result['metrics']['costs']['slippage']) == pytest.approx(1.38)


def test_gaps_and_zero_volume_never_fill_and_stale_marks_are_flagged():
    gap = run(dataset([10, 12, 16, 8, 8], missing=(2,)))
    assert all(row['session'] != gap['rows'][2]['session'] for row in gap['ledger'])
    assert any(event['reason'] == 'missing_raw_bar' for event in gap['execution_events'])
    zero = run(dataset([10, 12, 16, 17, 18], volumes=[100, 100, 0, 100, 100]))
    assert zero['ledger'][0]['session'] == zero['rows'][3]['session']
    assert any(event['reason'] == 'zero_or_missing_volume' for event in zero['execution_events'])


def test_adjusted_feature_basis_keeps_raw_fill_and_marks():
    from tests.unit.test_research_screening import merge
    raw = dataset([10, 12, 16, 8, 8], opens=[10, 12, 15, 8, 8])
    adjusted = dataset([20, 24, 32, 16, 16], basis='provider_adjusted', adjustments=('split',))
    data = merge(raw, adjusted)
    result = run(data, feature_series_keys=[adjusted.series[0].key])
    assert float(result['ledger'][0]['price']) == 15
    assert float(result['rows'][2]['nav']) == 106
    assert result['feature_series_keys'] == [adjusted.series[0].key]


def test_unsupported_actions_and_strict_unknown_tradability_fail_before_run():
    data = dataset([10, 11, 12])
    with pytest.raises(UnsupportedResearchInput, match='corporate'):
        run(data, parameters={'corporate_actions': [{'type': 'split'}]})
    with pytest.raises(UnsupportedResearchInput, match='tradability'):
        run(data, parameters={'tradability_profile': 'strict'})


def test_float64_numeric_admission_preserves_input_and_rejects_range_loss():
    from backtest_research.engines import admit_float64
    exact = Decimal('10.000000000000000000000001')
    assert admit_float64(exact) == 10.0
    assert exact == Decimal('10.000000000000000000000001')
    with pytest.raises(UnsupportedResearchInput, match='finite'):
        admit_float64(Decimal('1e1000'))
    with pytest.raises(UnsupportedResearchInput, match='underflow'):
        admit_float64(Decimal('1e-1000'))


def test_prior_framework_results_do_not_change_when_future_appended():
    short = run(dataset([10, 12, 16, 8, 8]))
    longer = run(dataset([10, 12, 16, 8, 8, 12, 13]))
    assert short['rows'] == longer['rows'][:5]
    assert short['ledger'] == [row for row in longer['ledger'] if row['session'] <= short['rows'][-1]['session']]


def test_simultaneous_rotation_sells_before_buying_with_framework_cash():
    from backtest_research.strategies import StrategyDecision, StrategyDefinition, register_strategy
    from tests.unit.test_research_screening import merge
    class Rotation:
        definition = StrategyDefinition('engine_test_rotation', '1', ('price',))
        def decide(self, features, portfolio, previous_state, parameters):
            keys = list(features)
            calls = previous_state.get('calls', 0)
            target = keys[0] if calls == 0 else keys[1]
            return StrategyDecision({key: '1' if key == target else '0' for key in keys},
                                    ('rotation',), tuple(keys), {'calls': calls + 1})
    register_strategy(Rotation())
    data = merge(dataset([10, 10, 10, 10], security='A'), dataset([20, 20, 20, 20], security='B'))
    result = run(data, strategy='engine_test_rotation')
    assert [(row['security_id'], row['quantity']) for row in result['ledger']] == [('A', 10), ('A', -10), ('B', 5)]
    assert result['ledger'][1]['session'] == result['ledger'][2]['session']
    assert float(result['rows'][-1]['cash']) == 0
    assert float(result['rows'][-1]['nav']) == 100


def test_known_halts_block_and_missing_marks_carry_with_explicit_flags():
    data = dataset([10, 12, 16, 17, 18])
    result = run(data, parameters={'known_halts': [{'security_id': 'A', 'session': data.sessions[2].isoformat()}]})
    assert result['ledger'][0]['session'] == data.sessions[3].isoformat()
    assert result['execution_events'][0]['reason'] == 'known_halt'
    missing = run(dataset([10, 12, 16, 17, 18], missing=(3,)))
    assert missing['rows'][3]['stale_mark_series_keys']
    assert missing['rows'][3]['nav'] == missing['rows'][2]['nav']
    assert 'stale_mark_carrying' in missing['quality_gaps']


def test_rsi_time_exit_counts_actual_eligible_holding_sessions():
    data = dataset([10, 9] + [8] * 8, volumes=[100, 100, 100, 100, 0, 100, 100, 100, 100, 100])
    result = run(data, strategy='mean_reversion_rsi2')
    assert result['ledger'][0]['session'] == data.sessions[3].isoformat()
    assert result['ledger'][1]['session'] == data.sessions[9].isoformat()
    assert result['rows'][4]['positions'][data.series[0].key]['held_sessions'] == 1


def test_stateful_extension_cannot_rewrite_prior_decision_states():
    from backtest_research.strategies import StrategyDecision, StrategyDefinition, register_strategy
    class Stateful:
        definition = StrategyDefinition('engine_test_stateful', '1', ('price',))
        def decide(self, features, portfolio, previous_state, parameters):
            previous_state['calls'] = previous_state.get('calls', 0) + 1
            return StrategyDecision({key: '0' for key in features}, ('stateful',), tuple(features), previous_state)
    register_strategy(Stateful())
    short = run(dataset([10, 11]), strategy='engine_test_stateful')
    long = run(dataset([10, 11, 12]), strategy='engine_test_stateful')
    assert short['decisions'] == long['decisions'][:2]
    assert [row['new_state']['calls'] for row in long['decisions']] == [1, 2, 3]


def test_backtrader_default_and_trace_separate_queued_blocked_and_filled():
    data = dataset([10, 12, 16, 17], volumes=[100, 100, 0, 100])
    result = run_backtest(data, raw_series_keys=[data.series[0].key], initial_cash='100', parameters={'sma_period': 2})
    assert result['engine']['name'] == 'backtrader'
    assert result['decisions'][0]['selection_trace'][0]['feature_statuses']['sma_slow']['reason'] == 'warmup'
    queued, blocked, filled = result['selection_trace'][1:]
    assert queued['selected'] is True and queued['actual_holding_quantity'] == 0
    assert queued['pending_target']['status'] == 'queued_next_open'
    assert queued['actual_fills'] == []
    assert blocked['pending_target']['status'] == 'pending_unfilled'
    assert blocked['actual_fills'] == []
    assert blocked['execution_events'][0]['reason'] == 'zero_or_missing_volume'
    assert filled['actual_holding_quantity'] == result['ledger'][0]['quantity']
    assert filled['actual_fills'][0]['sequence'] == result['ledger'][0]['sequence']
    assert filled['pending_target'] is None
    assert filled['feature_values'] == {'sma_fast': '17', 'sma_slow': '16.5'}
    assert filled['raw_series_key'] == filled['feature_series_key'] == data.series[0].key
    assert all(ref['session'] <= filled['session'] for ref in filled['input_observation_refs'])


def momentum_data(length=6):
    from tests.unit.test_research_screening import merge
    return merge(dataset([10, 12, 13, 13, 13, 13][:length], security='A'),
                 dataset([10, 12, 24, 30, 36, 40][:length], security='B'),
                 dataset([10, 14, 15, 15, 15, 15][:length], security='C', missing=(2,)))


def momentum_run(data):
    return run(data, strategy='price_momentum', parameters={'momentum_lookback': 2, 'momentum_skip': 1,
                                                          'top_n': 1, 'rebalance_every': 2})


def test_actual_multiasset_momentum_rank_tie_exclusion_trace_and_rotation():
    data = momentum_data()
    result = momentum_run(data)
    at_two = {row['security_id']: row for row in result['decisions'][2]['selection_trace']}
    assert at_two['A']['rank'] == 1 and at_two['B']['rank'] == 2
    assert at_two['A']['feature_values']['momentum'] == at_two['B']['feature_values']['momentum'] == '0.2'
    assert at_two['A']['selected'] is True and at_two['B']['selected'] is False
    assert at_two['C']['eligible'] is False and at_two['C']['exclusion_reason'] == 'missing_session'
    assert at_two['A']['actual_holding_quantity'] == 0
    assert len(result['selection_trace']) == len(data.sessions) * 3
    at_three = {row['security_id']: row for row in result['decisions'][3]['selection_trace']}
    assert at_three['B']['candidate_selected'] is True and at_three['B']['selected'] is False
    assert at_three['A']['candidate_selected'] is False and at_three['A']['selected'] is True
    assert at_three['A']['actual_holding_quantity'] == 7
    assert at_three['A']['actual_fills'][0]['signal_session'] == data.sessions[2].isoformat()
    assert [(row['security_id'], row['quantity']) for row in result['ledger']] == [('A', 7), ('A', -7), ('B', 2)]
    assert result['ledger'][1]['session'] == result['ledger'][2]['session'] == data.sessions[5].isoformat()
    assert [float(row['nav']) for row in result['rows']] == [100] * 6
    assert result['metrics']['total_return']['value'] == 0
    assert replay_result(result, data)['result_id'] == result['result_id']


def test_momentum_selection_trace_and_decisions_are_prefix_invariant():
    short, long = momentum_run(momentum_data(5)), momentum_run(momentum_data(6))
    assert short['decisions'] == long['decisions'][:5]
    assert short['selection_trace'] == long['selection_trace'][:15]
    assert short['rows'] == long['rows'][:5]


def test_momentum_unused_slots_stay_cash_and_lineage_matches_input_catalog():
    from tests.unit.test_research_screening import merge
    from backtest_research.contracts import canonical_hash
    data = merge(dataset([10, 12, 13, 14], security='A'), dataset([10, 10, 10, 10], security='B'))
    result = run(data, strategy='price_momentum', parameters={'momentum_lookback': 2, 'momentum_skip': 1,
                                                           'top_n': 2, 'rebalance_every': 1})
    trace = result['decisions'][2]['selection_trace']
    assert trace[0]['target_weight'] == '0.5'
    assert trace[1]['exclusion_reason'] == 'nonpositive_momentum'
    assert result['ledger'][0]['quantity'] == 3
    assert float(result['rows'][-1]['cash']) == 58
    fields = ('series_key', 'session', 'observation_id', 'input_hash', 'raw_id', 'decision_id')
    for row in result['selection_trace']:
        window = row['feature_input_windows']['momentum']
        refs = [{field: bar[field] for field in fields} for bar in result['input_observation_catalog']
                if bar['series_key'] == window['series_key'] and window['start_session'] <= bar['session'] <= window['end_session']]
        refs.sort(key=lambda bar: bar['session'])
        assert canonical_hash(refs) == window['input_refs_hash']
        assert all(bar['session'] <= row['session'] for bar in row['input_observation_refs'])
