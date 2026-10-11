import json
from decimal import Decimal
import pytest
from backtest_research.results import ImmutableResultStore, seal_result, verify_result


def test_hash_preserved_on_disk_and_tampering_rejected(tmp_path):
    result = seal_result({'dataset_id': 'x', 'rows': [{'nav': '100'}], 'fingerprint': {'strategy': 'v1'}})
    store = ImmutableResultStore(tmp_path)
    assert store.put(result) == result['result_id']
    assert store.put(result) == result['result_id']
    assert store.get(result['result_id']) == result
    path = tmp_path / (result['result_id'] + '.json')
    altered = json.loads(path.read_text())
    altered['rows'][0]['nav'] = '200'
    path.write_text(json.dumps(altered))
    with pytest.raises(ValueError, match='hash'):
        store.get(result['result_id'])


def test_hash_does_not_accept_nonfinite_and_identifier_is_bounded(tmp_path):
    with pytest.raises(ValueError, match='finite'):
        seal_result({'nav': float('nan')})
    with pytest.raises(ValueError, match='identifier'):
        ImmutableResultStore(tmp_path).get('../secret')


def test_sealed_extension_state_is_json_safe_and_exact():
    result = seal_result({'state': {'exact': Decimal('1.00000000000000000001')}})
    encoded = json.loads(json.dumps(result))
    assert encoded['state']['exact'] == {'$decimal': '1.00000000000000000001'}
    assert verify_result(encoded)


def test_failed_write_never_exposes_a_partial_result(tmp_path, monkeypatch):
    import backtest_research.results as module
    result = seal_result({'dataset_id': 'x', 'rows': [{'nav': '100'}]})
    def fail_sync(descriptor):
        raise OSError('simulated storage failure')
    monkeypatch.setattr(module.os, 'fsync', fail_sync)
    with pytest.raises(OSError, match='storage'):
        ImmutableResultStore(tmp_path).put(result)
    assert not (tmp_path / (result['result_id'] + '.json')).exists()


def test_strategy_extension_protocol_works_without_framework_import():
    from backtest_research.strategies import StrategyDecision, StrategyDefinition, register_strategy, get_strategy

    class Extension:
        definition = StrategyDefinition('test_extension', '1', ('price',))
        def decide(self, features, portfolio, previous_state, parameters):
            return StrategyDecision({'A': '0'}, ('extension_probe',), ('input-ref',), {'calls': previous_state.get('calls', 0) + 1})
    plugin = Extension()
    register_strategy(plugin)
    decision = get_strategy('test_extension').decide({}, {}, {}, {})
    assert decision.new_state['calls'] == 1
    assert decision.reason_codes == ('extension_probe',)
    with pytest.raises(ValueError, match='registered'):
        register_strategy(plugin)


def test_builtin_strategies_preserve_unavailable_targets_and_actual_holding_clock():
    from backtest_research.strategies import get_strategy
    trend = get_strategy('trend_sma')
    assert trend.decide({'A': {'sma_fast': '12', 'sma_slow': '11'}}, {}, {}, {}).target_weights == {'A': '1'}
    assert trend.decide({'A': {'sma_fast': None, 'sma_slow': None}}, {}, {'targets': {'A': '1'}}, {}).target_weights == {'A': '1'}
    rsi = get_strategy('mean_reversion_rsi2')
    entry = rsi.decide({'A': {'rsi': '4'}}, {'A': {'quantity': 0, 'held_sessions': 0}}, {}, {})
    assert entry.target_weights == {'A': '1'}
    holding = rsi.decide({'A': {'rsi': '20'}}, {'A': {'quantity': 1, 'held_sessions': 4}}, entry.new_state, {})
    assert holding.target_weights == {'A': '1'}
    exit_decision = rsi.decide({'A': {'rsi': '20'}}, {'A': {'quantity': 1, 'held_sessions': 5}}, holding.new_state, {})
    assert exit_decision.target_weights == {'A': '0'}
    assert rsi.decide({'A': {'rsi': None}}, {'A': {'quantity': 1}}, exit_decision.new_state, {}).target_weights == {'A': '0'}
