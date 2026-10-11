"""Own-data daily adapter; Backtrader exclusively owns fills and accounting."""
from datetime import datetime, time
from copy import deepcopy
from decimal import Context, Decimal, ROUND_FLOOR, localcontext
import hashlib
import math
from pathlib import Path
import platform
import backtrader as bt
from backtest_research.contracts import canonical_hash, canonical_value
from backtest_research.evaluation import calculate_metrics
from backtest_research.features import compute_price_features
from backtest_research.results import seal_result
from backtest_research.strategies import get_strategy, strategy_fingerprint
from . import UnsupportedResearchInput, admit_float64


ADAPTER_VERSION = 'backtrader_own_dataset_next_open_v2'


def _number(value):
    converted = float(value)
    if not math.isfinite(converted):
        raise UnsupportedResearchInput('framework produced nonfinite accounting')
    return str(Decimal(str(converted)))


def _code_hash():
    root = Path(__file__).resolve().parents[1]
    # Bind the actual adapter, features, contracts, plugins and evaluation code.
    names = ('contracts.py', 'features/__init__.py', 'strategies/__init__.py',
             'evaluation/__init__.py', 'engines/__init__.py', 'engines/backtrader_adapter.py', 'results.py')
    return canonical_hash({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names})


def execute(dataset, parameters, raw_keys, feature_keys, cash, fee, slippage, *, strategy):
    with localcontext(Context(prec=28)):
        return _execute(dataset, parameters, raw_keys, feature_keys, cash, fee, slippage, strategy=strategy)


def _execute(dataset, parameters, raw_keys, feature_keys, cash, fee, slippage, *, strategy):
    plugin = get_strategy(strategy)
    fast = parameters.get('fast_period', 1 if 'sma_period' in parameters else 50)
    slow = parameters.get('slow_period', parameters.get('sma_period', 200))
    rsi_period = parameters.get('rsi_period', 2)
    if any(type(n) is not int or not 1 <= n <= 6000 for n in (fast, slow, rsi_period)):
        raise UnsupportedResearchInput('invalid strategy feature periods')
    by_key = {s.key: s for s in dataset.series}
    feature_by_security = {by_key[key].security_id: key for key in feature_keys}
    raw_bars = {key: {bar.session: bar for bar in dataset.series_bars(key)} for key in raw_keys}
    feature_bars = {key: {bar.session: bar for bar in dataset.series_bars(key)} for key in feature_keys}
    signals, definitions = {}, {}
    feature_parameters = dict(rsi_period=rsi_period, momentum_lookback=parameters.get('momentum_lookback', 252),
                              momentum_skip=parameters.get('momentum_skip', 21))
    for key in raw_keys:
        feature_key = feature_by_security[by_key[key].security_id]
        first = compute_price_features(dataset, feature_key, sma_period=fast, **feature_parameters)
        second = compute_price_features(dataset, feature_key, sma_period=slow, **feature_parameters)
        signals[key] = {session: dict(row, sma_fast=row['sma'], sma_slow=other['sma'], feature_series_key=feature_key,
                                     security_id=by_key[key].security_id,
                                     statuses=dict(row['statuses'], sma_fast=row['statuses']['sma'], sma_slow=other['statuses']['sma']))
                        for session, row, other in zip(dataset.sessions, first['rows'], second['rows'])}
        definitions[key] = {'fast': first['definition_hash'], 'slow': second['definition_hash']}
    halted = set()
    for event in parameters.get('known_halts', []):
        if not isinstance(event, dict) or set(event) != {'security_id', 'session'}:
            raise UnsupportedResearchInput('explicit dated known halt identity required')
        halted.add((event['security_id'], event['session']))
    code_hash = _code_hash()
    snapshots, ledger, events, decisions, selection_trace = [], [], [], [], []

    def observation_ref(bar):
        return dict(series_key=bar.series.key, session=bar.session.isoformat(), observation_id=bar.observation_id,
                    input_hash=bar.input_hash, raw_id=bar.raw_id, decision_id=bar.decision_id)

    def feature_lineage(key, index):
        selected_key = feature_by_security[by_key[key].security_id]
        observations = feature_bars[selected_key]
        windows, boundary_refs = {}, {}
        for name in plugin.definition.required_features:
            size = {'sma_fast': fast, 'sma_slow': slow, 'rsi': rsi_period + 1,
                    'momentum': feature_parameters['momentum_lookback'] + 1,
                    'volatility': 21, 'simple_return': 2}.get(name, 1)
            start = max(0, index - size + 1)
            if name == 'rsi':
                # Wilder recurrence consumes every change since the last gap.
                start = index
                while start > 0 and dataset.sessions[start - 1] in observations:
                    start -= 1
            grid = dataset.sessions[start:index + 1]
            present = [observations[session] for session in grid if session in observations]
            windows[name] = dict(series_key=selected_key, start_session=grid[0].isoformat(), end_session=grid[-1].isoformat(),
                                 required_initial_sessions=size, declared_session_count=len(grid), observation_count=len(present),
                                 input_refs_hash=canonical_hash([observation_ref(bar) for bar in present]),
                                 reference_scope='catalog_window_with_explicit_missing_sessions')
            boundaries = (start, index)
            if name == 'momentum' and index >= feature_parameters['momentum_skip']:
                boundaries += (index - feature_parameters['momentum_skip'],)
            for offset in boundaries:
                session = dataset.sessions[offset]
                if session in observations:
                    ref = observation_ref(observations[session])
                    boundary_refs[(ref['series_key'], ref['session'])] = ref
        return windows, [boundary_refs[key] for key in sorted(boundary_refs)]

    class DatasetFeed(bt.feed.DataBase):
        params = (('grid', None), ('bars', None))

        def start(self):
            super().start()
            self._sessions = iter(self.p.grid)
            self._last_close = 0.0  # No position exists before the first valid bar.

        def _load(self):
            session = next(self._sessions, None)
            if session is None:
                return False
            bar = self.p.bars.get(session)
            self.lines.datetime[0] = bt.date2num(datetime.combine(session, time()))
            if bar is not None:
                self._last_close = admit_float64(bar.close)
                for name in ('open', 'high', 'low', 'close'):
                    getattr(self.lines, name)[0] = admit_float64(getattr(bar, name))
                self.lines.volume[0] = admit_float64(bar.volume) if bar.volume is not None else 0
            else:
                self.lines.open[0] = float('nan')
                self.lines.high[0] = self._last_close
                self.lines.low[0] = self._last_close
                self.lines.close[0] = self._last_close
                self.lines.volume[0] = 0
            self.lines.openinterest[0] = 0
            return True

    class ResearchStrategy(bt.Strategy):
        def __init__(self):
            self.pending_targets, self.last_targets, self.state = {}, {}, {}
            self.held_sessions = {key: 0 for key in raw_keys}
            self.feeds = {feed._name: feed for feed in self.datas}
            self.submitted_targets = {}
            self.order_sequence = 0
            self.running_peak = float(cash)

        def _event(self, session, key, reason, **details):
            events.append(dict(session=session.isoformat(), security_id=by_key[key].security_id,
                               raw_series_key=key, reason=reason, **details))

        def next_open(self):
            session = self.data.datetime.date(0)
            if not self.pending_targets:
                return
            available, desired, positions = {}, {}, {}
            equity = Decimal(str(self.broker.getcash()))
            for key in raw_keys:
                position = self.getposition(self.feeds[key]).size
                positions[key] = int(position)
                bar = raw_bars[key].get(session)
                mark = bar.open if bar else Decimal(str(self.feeds[key].close[-1]))
                equity += position * mark
            for key, pending in sorted(self.pending_targets.items()):
                bar = raw_bars[key].get(session)
                if bar is None:
                    self._event(session, key, 'missing_raw_bar')
                    continue
                if (by_key[key].security_id, session.isoformat()) in halted:
                    self._event(session, key, 'known_halt')
                    continue
                if bar.volume is None or bar.volume <= 0:
                    self._event(session, key, 'zero_or_missing_volume')
                    continue
                available[key] = bar
                weight = Decimal(pending['weight'])
                buy_price = bar.open * (1 + slippage / 10000)
                desired[key] = int((equity * weight / buy_price).to_integral_value(rounding=ROUND_FLOOR))
                if desired[key] > 2 ** 53 - 1:
                    raise UnsupportedResearchInput('integer quantity exceeds exact float64 range')
            budget = Decimal(str(self.broker.getcash()))
            # Queue sells first. The framework processes its orders in this order.
            for key in raw_keys:
                if key not in desired:
                    continue
                difference = desired[key] - positions[key]
                if difference < 0:
                    price = available[key].open * (1 - slippage / 10000)
                    budget += -difference * price * (1 - fee / 10000)
                    self._submit(key, -difference, False, session)
                elif difference == 0:
                    self.pending_targets.pop(key, None)
            for key in raw_keys:
                if key not in desired:
                    continue
                difference = desired[key] - positions[key]
                if difference <= 0:
                    continue
                price = available[key].open * (1 + slippage / 10000)
                unit_cost = price * (1 + fee / 10000)
                affordable = max(0, int((budget / unit_cost).to_integral_value(rounding=ROUND_FLOOR)))
                count = min(difference, affordable)
                if count < difference:
                    self._event(session, key, 'cash_shortage', requested_quantity=difference, filled_quantity=count)
                if count:
                    budget -= count * unit_cost
                    self._submit(key, count, True, session)
                else:
                    self._event(session, key, 'integer_cash_dust')
                    self.pending_targets.pop(key, None)

        def _submit(self, key, count, buy, session):
            pending = self.pending_targets[key]
            self.order_sequence += 1
            order = (self.buy if buy else self.sell)(data=self.feeds[key], size=count, exectype=bt.Order.Market)
            self.submitted_targets[order.ref] = dict(key=key, signal_session=pending['signal_session'],
                                                    sequence=self.order_sequence, weight=pending['weight'])

        def notify_order(self, order):
            if order.status not in (order.Completed, order.Canceled, order.Margin, order.Rejected):
                return
            instruction = self.submitted_targets.pop(order.ref, None)
            if instruction is None:
                return
            key, session = instruction['key'], bt.num2date(order.executed.dt).date() if order.status == order.Completed else self.data.datetime.date(0)
            if order.status == order.Completed:
                bar = raw_bars[key][session]
                quantity = int(order.executed.size)
                price = order.executed.price
                ledger.append(dict(session=session.isoformat(), signal_session=instruction['signal_session'],
                    sequence=instruction['sequence'], security_id=by_key[key].security_id, raw_series_key=key,
                    quantity=quantity, price=_number(price), raw_open=str(bar.open), fee=_number(order.executed.comm),
                    slippage_cost=_number(abs(quantity) * abs(price - float(bar.open))), tax='0',
                    observed_tradability='unknown', execution_assumption='ex_post_positive_daily_volume',
                    observation_id=bar.observation_id, input_hash=bar.input_hash))
                self.pending_targets.pop(key, None)
                if self.getposition(order.data).size == 0:
                    self.held_sessions[key] = 0
            else:
                self._event(session, key, 'framework_' + order.getstatusname().lower())

        def next(self):
            session = self.data.datetime.date(0)
            portfolio, stale = {}, []
            for key in raw_keys:
                quantity = int(self.getposition(self.feeds[key]).size)
                bar = raw_bars[key].get(session)
                if quantity:
                    eligible = (bar is not None and bar.volume is not None and bar.volume > 0 and
                                (by_key[key].security_id, session.isoformat()) not in halted)
                    if eligible:
                        self.held_sessions[key] += 1
                    if bar is None:
                        stale.append(key)
                else:
                    self.held_sessions[key] = 0
                portfolio[key] = dict(quantity=quantity, held_sessions=self.held_sessions[key])
            nav = self.broker.getvalue()
            self.running_peak = max(self.running_peak, nav)
            snapshots.append(dict(session=session.isoformat(), nav=_number(nav), cash=_number(self.broker.getcash()),
                                  drawdown=_number(nav / self.running_peak - 1),
                                  exposure=_number((nav - self.broker.getcash()) / nav) if nav > 0 else None,
                                  positions=portfolio, stale_mark_series_keys=stale))
            feature_view = {key: signals[key][session] for key in raw_keys}
            decision = plugin.decide(deepcopy(feature_view), deepcopy(portfolio), deepcopy(self.state), deepcopy(parameters))
            weights = {key: Decimal(str(weight)) for key, weight in decision.target_weights.items()}
            if set(weights) != set(raw_keys) or any(not weight.is_finite() or not 0 <= weight <= 1 for weight in weights.values()) or sum(weights.values()) > 1:
                raise UnsupportedResearchInput('strategy must return bounded long-only weights for selected identities')
            queued_now = set()
            for key, weight in weights.items():
                rebalance = decision.rebalance and (weight > 0 or portfolio[key]['quantity'] > 0 or key in self.pending_targets)
                if weight != Decimal(str(self.last_targets.get(key, 0))) or rebalance:
                    self.pending_targets[key] = {'weight': str(weight), 'signal_session': session.isoformat()}
                    queued_now.add(key)
            self.last_targets, self.state = {key: str(weight) for key, weight in weights.items()}, deepcopy(dict(decision.new_state))
            traces = []
            index = len(snapshots) - 1
            for key in raw_keys:
                feature = feature_view[key]
                names = plugin.definition.required_features
                values = {name: feature.get(name) for name in names}
                statuses = {name: deepcopy(feature['statuses'].get(name, {'status': 'unavailable', 'reason': 'undeclared_feature'})) for name in names}
                excluded = next((status['reason'] for status in statuses.values() if status['status'] != 'available'), None)
                metadata = (decision.selection_details or {}).get(key, {})
                pending = self.pending_targets.get(key)
                windows, refs = feature_lineage(key, index)
                key_reasons = [reason[len(key) + 1:] for reason in decision.reason_codes if reason.startswith(key + ':')]
                if not key_reasons:
                    key_reasons = list(decision.reason_codes)
                traces.append(dict(session=session.isoformat(), security_id=by_key[key].security_id,
                    raw_series_key=key, feature_series_key=feature['feature_series_key'],
                    feature_values=values, feature_statuses=statuses, feature_input_windows=windows,
                    input_observation_refs=refs, input_reference_scope='window_boundaries_and_current_observation',
                    target_weight=str(weights[key]), selected=weights[key] > 0,
                    eligible=metadata.get('eligible', excluded is None), exclusion_reason=metadata.get('exclusion_reason', excluded),
                    rank=metadata.get('rank'), score=metadata.get('score'),
                    candidate_selected=metadata.get('candidate_selected', weights[key] > 0 and excluded is None),
                    rebalance_due=metadata.get('rebalance_due'), decision_reasons=key_reasons,
                    pending_target=(dict(status='queued_next_open' if key in queued_now else 'pending_unfilled',
                                         signal_session=pending['signal_session'], target_weight=pending['weight']) if pending else None),
                    actual_fills=deepcopy([trade for trade in ledger if trade['session'] == session.isoformat() and trade['raw_series_key'] == key]),
                    execution_events=deepcopy([event for event in events if event['session'] == session.isoformat() and event['raw_series_key'] == key]),
                    actual_holding_quantity=portfolio[key]['quantity'], actual_held_sessions=portfolio[key]['held_sessions'],
                    actual_holding_weight=_number(portfolio[key]['quantity'] * self.feeds[key].close[0] / nav) if nav > 0 else None))
            selection_trace.extend(deepcopy(traces))
            decisions.append(dict(session=session.isoformat(), target_weights=deepcopy(self.last_targets),
                                  reason_codes=list(decision.reason_codes), input_refs=list(decision.input_refs),
                                  new_state=deepcopy(self.state), selection_trace=traces))

    cerebro = bt.Cerebro(cheat_on_open=True, stdstats=False, preload=False)
    cerebro.broker.setcash(admit_float64(cash))
    cerebro.broker.setcommission(commission=admit_float64(fee / 10000), stocklike=True, percabs=True)
    cerebro.broker.set_slippage_perc(admit_float64(slippage / 10000), slip_open=True, slip_match=True, slip_out=True)
    # Adapter reserves cash using next-open modeled prices; broker performs the
    # final cash/margin check on actual execution, after earlier sells.
    cerebro.broker.set_checksubmit(False)
    for key in raw_keys:
        cerebro.adddata(DatasetFeed(grid=dataset.sessions, bars=raw_bars[key]), name=key)
    cerebro.addstrategy(ResearchStrategy)
    instances = cerebro.run(runonce=False)
    if len(instances) != 1 or len(snapshots) != len(dataset.sessions):
        raise UnsupportedResearchInput('framework did not execute the complete declared session grid')
    if code_hash != _code_hash():
        raise UnsupportedResearchInput('research code changed during execution')
    gaps = set(dataset.quality_gaps) | {'corporate_action_coverage_unknown', 'price_only_no_cash_dividends',
                                      'unknown_official_tradability', 'ex_post_daily_volume_execution_assumption',
                                      'subset_selection_bias', 'historical_universe_unverified'}
    if dataset.calendar_grade == 'observed_session_union':
        gaps.add('market_calendar_unverified')
    if any(row['stale_mark_series_keys'] for row in snapshots):
        gaps.add('stale_mark_carrying')
    run_config = dict(raw_series_keys=raw_keys, feature_series_keys=feature_keys, strategy=strategy,
                      parameters=parameters, initial_cash=str(cash), fee_bps=str(fee), slippage_bps=str(slippage), engine='backtrader')
    profile = dict(version='modeled_next_open_price_only_v1', corporate_action_policy='price_only_research',
                   tradability_profile='research_daily_tradability_v1', tradability_observed='unknown',
                   daily_volume_use='ex_post_execution_gate_only', sizing='next_open_integer_cash_constrained',
                   allocation_schedule='periodic_rebalance' if strategy == 'price_momentum' else 'desired_state_changes_only', sell_before_buy=True,
                   buying_order='stable_security_id', slippage_price='raw_open_times_one_plus_or_minus_bps',
                   commission='modeled_notional_bps', tax='not_modeled_explicit_zero',
                   assumed_trade_count=len(ledger), assumed_security_count=len({row['security_id'] for row in ledger}),
                   realistic_costs_verified=False, currency=by_key[raw_keys[0]].currency, market=by_key[raw_keys[0]].market,
                   calendar_grade=dataset.calendar_grade, stale_mark_policy='carry_last_raw_close_never_fill',
                   float64_admission_relative_tolerance='1e-12', float64_admission_absolute_tolerance='1e-12')
    fingerprint = dict(input_hash=dataset.content_hash, manifest_hash=dataset.manifest_hash, code_hash=code_hash,
                       strategy_definition_hash=strategy_fingerprint(plugin, parameters), feature_definition_hashes=definitions,
                       engine='backtrader', engine_version=bt.__version__, adapter_version=ADAPTER_VERSION,
                       python_version=platform.python_version(), execution_definition_hash=canonical_hash(profile))
    return seal_result(dict(dataset_id=dataset.dataset_id, original_target_count=dataset.original_target_count,
        manifest_id=dataset.manifest_id, manifest_hash=dataset.manifest_hash, origin=dataset.origin,
        cutoff=dataset.cutoff.isoformat(), source_snapshot_at=dataset.source_snapshot_at.isoformat(),
        engine=dict(name='backtrader', version=bt.__version__, adapter_version=ADAPTER_VERSION,
                    actual_framework_execution=True, qualification_scope='daily_price_only_research'),
        run_config=run_config, raw_series_keys=raw_keys, feature_series_keys=feature_keys,
        series_identities={key: canonical_value(by_key[key]) for key in raw_keys + feature_keys},
        fingerprint=fingerprint, rows=snapshots, ledger=ledger, execution_events=events, decisions=decisions,
        selection_trace=selection_trace,
        selection_trace_policy=dict(version='session_security_selection_trace_v1', universe='declared_dataset_subset',
            decision_as_of='session_close', pending_execution='next_eligible_raw_open', actual_fills_as_of='same_session',
            candidate_selection_is_holding=False, rank_policy='descending_score_then_security_id_then_raw_series_key',
            rebalance_every=parameters.get('rebalance_every', 21) if strategy == 'price_momentum' else None,
            rebalance_anchor='declared_session_grid_index_zero' if strategy == 'price_momentum' else None,
            allocation_slots=parameters.get('top_n', 20) if strategy == 'price_momentum' else None),
        input_observation_catalog=[dict(observation_ref(bar), captured_at=bar.captured_at.isoformat(), available_at=bar.available_at.isoformat())
                                   for bar in dataset.bars if bar.series.key in set(raw_keys + feature_keys)],
        metrics=calculate_metrics(snapshots, ledger), quality_gaps=sorted(gaps), execution_profile=profile,
        evaluation_scope='in_sample_research', historical_universe_verified=False))
