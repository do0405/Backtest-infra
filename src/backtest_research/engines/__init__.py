"""Explicit framework selection and admission policy. No internal engine fallback."""
from decimal import Decimal
import importlib.metadata
import math


class EngineUnavailable(RuntimeError):
    pass


class UnsupportedResearchInput(ValueError):
    pass


def admit_float64(value):
    """Validate conversion; retain the exact Decimal input and its original hash."""
    value = Decimal(str(value))
    converted = float(value)
    if not value.is_finite() or not math.isfinite(converted):
        raise UnsupportedResearchInput('finite float64 admission required')
    difference = abs(Decimal.from_float(converted) - value)
    if difference > max(Decimal('1e-12'), abs(value) * Decimal('1e-12')):
        raise UnsupportedResearchInput('float64 admission exceeds fixed tolerance')
    if value != 0 and converted == 0:
        raise UnsupportedResearchInput('float64 admission underflow')
    return converted


def validate_run(dataset, raw_series_keys, feature_series_keys, parameters, initial_cash, fee_bps, slippage_bps):
    parameters = dict(parameters or {})
    if parameters.get('corporate_actions') or parameters.get('corporate_action_policy', 'price_only_research') != 'price_only_research':
        raise UnsupportedResearchInput('corporate actions are unsupported by the price-only adapter')
    if parameters.get('tradability_profile', 'research_daily_tradability_v1') != 'research_daily_tradability_v1':
        raise UnsupportedResearchInput('strict tradability is unsupported without official observations')
    identities = {s.key: s for s in dataset.series}
    raw_series_keys = list(raw_series_keys)
    if not raw_series_keys or len(raw_series_keys) != len(set(raw_series_keys)):
        raise UnsupportedResearchInput('nonempty unique raw series selection required')
    if any(key not in identities or identities[key].basis != 'as_traded' for key in raw_series_keys):
        raise UnsupportedResearchInput('explicit raw as-traded fill and mark series required')
    selected = [identities[key] for key in raw_series_keys]
    if len({s.security_id for s in selected}) != len(selected):
        raise UnsupportedResearchInput('one raw series per security required')
    if len({(s.market, s.currency) for s in selected}) != 1:
        raise UnsupportedResearchInput('single market and currency required; verified FX is unavailable')
    if any(s.quantity_unit != 'shares' for s in selected):
        raise UnsupportedResearchInput('integer share units required')
    features = list(raw_series_keys if feature_series_keys is None else feature_series_keys)
    if any(key not in identities for key in features) or len(features) != len(selected) or len(set(features)) != len(features):
        raise UnsupportedResearchInput('one explicitly selected feature series per security required')
    if {identities[key].security_id for key in features} != {s.security_id for s in selected}:
        raise UnsupportedResearchInput('feature and raw security identities must match')
    # A matching security alone does not prove comparable source/quantity/currency units.
    by_security = {identities[key].security_id: identities[key] for key in features}
    for raw in selected:
        feature = by_security[raw.security_id]
        if (feature.market, feature.currency, feature.quantity_unit) != (raw.market, raw.currency, raw.quantity_unit):
            raise UnsupportedResearchInput('incompatible feature price units')
    cash, fee, slippage = (Decimal(str(value)) for value in (initial_cash, fee_bps, slippage_bps))
    if not all(value.is_finite() for value in (cash, fee, slippage)) or cash <= 0 or not 0 <= fee <= 1000 or not 0 <= slippage <= 1000:
        raise UnsupportedResearchInput('invalid bounded cash or cost parameters')
    if not Decimal(0) <= Decimal(str(parameters.get('allocation', 1))) <= 1:
        raise UnsupportedResearchInput('allocation must be between zero and one')
    for bar in dataset.bars:
        if bar.series.key in raw_series_keys:
            for value in (bar.open, bar.high, bar.low, bar.close, bar.volume):
                if value is not None:
                    admit_float64(value)
    return parameters, sorted(raw_series_keys, key=lambda key: identities[key].security_id), sorted(features, key=lambda key: identities[key].security_id), cash, fee, slippage


def run_backtest(dataset, *, raw_series_keys, feature_series_keys=None, strategy='trend_sma', parameters=None,
                 initial_cash='100000', fee_bps='0', slippage_bps='0', engine='backtrader'):
    admitted = validate_run(dataset, raw_series_keys, feature_series_keys, parameters, initial_cash, fee_bps, slippage_bps)
    if engine == 'backtrader':
        try:
            version = importlib.metadata.version('backtrader')
        except importlib.metadata.PackageNotFoundError:
            raise EngineUnavailable('Backtrader 1.9.78.123 runtime unavailable') from None
        if version != '1.9.78.123':
            raise EngineUnavailable('unsupported Backtrader version')
        from .backtrader_adapter import execute
        return execute(dataset, *admitted, strategy=strategy)
    if engine != 'zipline-reloaded':
        raise EngineUnavailable('framework is not selected and qualified')
    try:
        version = importlib.metadata.version('zipline-reloaded')
    except importlib.metadata.PackageNotFoundError:
        raise EngineUnavailable('Zipline-reloaded 3.1.1 runtime unavailable; no fallback selected') from None
    if version != '3.1.1':
        raise EngineUnavailable('unsupported Zipline-reloaded version')
    raise EngineUnavailable('Zipline adapter is not qualified on this host')
