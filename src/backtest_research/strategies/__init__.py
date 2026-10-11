"""Framework-independent strategy extension protocol and initial plugins."""
from dataclasses import dataclass
from decimal import Decimal
import inspect
from typing import Mapping, Protocol
from backtest_research.contracts import canonical_hash


@dataclass(frozen=True)
class StrategyDefinition:
    name: str
    version: str
    required_features: tuple[str, ...]
    schedule: str = 'session_close_decision_next_eligible_open'


@dataclass(frozen=True)
class StrategyDecision:
    target_weights: Mapping[str, str]
    reason_codes: tuple[str, ...]
    input_refs: tuple[str, ...]
    new_state: Mapping
    selection_details: Mapping | None = None
    rebalance: bool = False


class StrategyPlugin(Protocol):
    definition: StrategyDefinition
    def decide(self, features, portfolio, previous_state, parameters) -> StrategyDecision: ...


_REGISTRY = {}


def register_strategy(plugin):
    definition = getattr(plugin, 'definition', None)
    if not isinstance(definition, StrategyDefinition) or not definition.name or not callable(getattr(plugin, 'decide', None)):
        raise ValueError('strategy extension protocol required')
    if definition.name in _REGISTRY:
        raise ValueError('strategy already registered')
    _REGISTRY[definition.name] = plugin
    return plugin


def get_strategy(name):
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError('unknown strategy') from None


def strategy_fingerprint(plugin, parameters):
    try:
        source = inspect.getsource(type(plugin))
    except (OSError, TypeError):
        raise ValueError('registered strategy must have inspectable source') from None
    return canonical_hash(dict(definition=plugin.definition, parameters=parameters, implementation=source))


class TrendSMA:
    definition = StrategyDefinition('trend_sma', '1', ('sma_fast', 'sma_slow'))

    def decide(self, features, portfolio, previous_state, parameters):
        weights, reasons = {}, []
        allocation = Decimal(str(parameters.get('allocation', 1))) / max(1, len(features))
        for key, feature in sorted(features.items()):
            if feature.get('sma_fast') is None or feature.get('sma_slow') is None:
                weights[key] = str(previous_state.get('targets', {}).get(key, 0))
                reasons.append(key + ':feature_unavailable')
            else:
                weights[key] = str(allocation if Decimal(feature['sma_fast']) > Decimal(feature['sma_slow']) else Decimal(0))
                reasons.append(key + ':trend_long' if Decimal(weights[key]) else key + ':trend_cash')
        return StrategyDecision(weights, tuple(reasons), tuple(features), {'targets': weights})


class MeanReversionRSI:
    definition = StrategyDefinition('mean_reversion_rsi2', '1', ('rsi',))

    def decide(self, features, portfolio, previous_state, parameters):
        weights, reasons = {}, []
        allocation = Decimal(str(parameters.get('allocation', 1))) / max(1, len(features))
        entry, exit_level = Decimal(str(parameters.get('entry_rsi', 5))), Decimal(str(parameters.get('exit_rsi', 50)))
        hold_limit = parameters.get('max_hold_sessions', 5)
        if type(hold_limit) is not int or hold_limit < 1 or not 0 <= entry < exit_level <= 100:
            raise ValueError('invalid RSI strategy parameters')
        for key, feature in sorted(features.items()):
            held = portfolio.get(key, {}).get('quantity', 0) > 0
            rsi = feature.get('rsi')
            if rsi is None:
                weight = previous_state.get('targets', {}).get(key, str(allocation) if held else '0')
                reason = 'feature_unavailable'
            elif held:
                exiting = Decimal(rsi) > exit_level or portfolio[key].get('held_sessions', 0) >= hold_limit
                weight, reason = ('0', 'exit') if exiting else (str(allocation), 'hold')
            else:
                weight, reason = (str(allocation), 'enter') if Decimal(rsi) < entry else ('0', 'cash')
            weights[key], reasons = str(weight), reasons + [key + ':' + reason]
        return StrategyDecision(weights, tuple(reasons), tuple(features), {'targets': weights})


class PriceMomentum:
    definition = StrategyDefinition('price_momentum', '1', ('momentum',),
                                    'periodic_declared_session_close_next_eligible_open')

    def decide(self, features, portfolio, previous_state, parameters):
        top_n, every = parameters.get('top_n', 20), parameters.get('rebalance_every', 21)
        positive_only = parameters.get('positive_only', True)
        if type(top_n) is not int or not 1 <= top_n <= 1000 or type(every) is not int or not 1 <= every <= 6000 or type(positive_only) is not bool:
            raise ValueError('invalid momentum ranking parameters')
        index = previous_state.get('session_index', -1) + 1
        due = index % every == 0
        candidates, details = [], {}
        for key, feature in features.items():
            status = feature.get('statuses', {}).get('momentum', {})
            score = feature.get('momentum')
            reason = status.get('reason', 'feature_unavailable') if score is None else None
            if score is not None and positive_only and Decimal(score) <= 0:
                reason = 'nonpositive_momentum'
            details[key] = dict(eligible=reason is None, exclusion_reason=reason, rank=None, score=score,
                                candidate_selected=False, rebalance_due=due)
            if reason is None:
                candidates.append(key)
        candidates.sort(key=lambda key: (-Decimal(features[key]['momentum']), features[key]['security_id'], key))
        for rank, key in enumerate(candidates, 1):
            details[key]['rank'] = rank
            details[key]['candidate_selected'] = rank <= top_n
        allocation = Decimal(str(parameters.get('allocation', 1))) / top_n
        weights, reasons = {}, []
        for key in features:
            metadata = details[key]
            if due:
                weights[key] = str(allocation if metadata['candidate_selected'] else Decimal(0))
                reason = 'momentum_selected' if metadata['candidate_selected'] else metadata['exclusion_reason'] or 'outside_top_n'
            else:
                weights[key] = str(previous_state.get('targets', {}).get(key, 0))
                reason = 'periodic_hold_target'
                if metadata['exclusion_reason']:
                    reasons.append(key + ':' + metadata['exclusion_reason'])
            reasons.append(key + ':' + reason)
        state = dict(targets=weights, session_index=index, rebalance_every=every,
                     last_rebalance_index=index if due else previous_state.get('last_rebalance_index'))
        return StrategyDecision(weights, tuple(reasons), tuple(features), state, details, due)


register_strategy(TrendSMA())
register_strategy(MeanReversionRSI())
register_strategy(PriceMomentum())
