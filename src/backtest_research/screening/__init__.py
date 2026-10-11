"""Stable rankings of the explicitly supplied subset, not a historical universe."""
from decimal import Decimal
from backtest_research.contracts import canonical_hash
from backtest_research.features import compute_price_features


def screen_subset(dataset, *, basis='as_traded', feature='momentum', parameters=None, top_n=20):
    if basis not in ('as_traded', 'provider_adjusted', 'split_only') or feature not in ('price', 'simple_return', 'sma', 'rsi', 'volatility', 'momentum'):
        raise ValueError('unsupported basis or feature')
    if type(top_n) is not int or not 1 <= top_n <= 1000:
        raise ValueError('bounded top_n required')
    parameters = dict(parameters or {})
    candidates, excluded, definitions = [], [], {}
    for security_id in dataset.selected_security_ids:
        series = [s for s in dataset.series if s.security_id == security_id and
                  ((s.basis == 'provider_adjusted' and s.adjustments == ('split',)) if basis == 'split_only' else s.basis == basis)]
        if len(series) != 1:
            excluded.append(dict(security_id=security_id, reason='requested_basis_unavailable' if not series else 'ambiguous_series_selection'))
            continue
        selected = series[0]
        calculated = compute_price_features(dataset, selected.key, **parameters)
        row = calculated['rows'][-1]
        score = row[feature]
        if score is None:
            excluded.append(dict(security_id=security_id, series_key=selected.key,
                                 reason=row['statuses'].get(feature, {}).get('reason', 'missing_session')))
            continue
        definitions[selected.key] = calculated['definition_hash']
        candidates.append(dict(security_id=security_id, series_key=selected.key, session=row['session'], score=str(score),
                               basis=selected.basis, adjustments=list(selected.adjustments)))
    candidates.sort(key=lambda row: (-Decimal(row['score']), row['security_id'], row['series_key']))
    for rank, row in enumerate(candidates, 1):
        row['rank'] = rank
    definition = dict(version='stable_subset_screen_v1', basis=basis, feature=feature,
                      parameters=parameters, top_n=top_n, feature_definition_hashes=definitions,
                      rank_policy='descending_score_then_security_id_then_series_key')
    return dict(dataset_id=dataset.dataset_id, definition_hash=canonical_hash(definition), definitions=definition,
                rows=candidates[:top_n], eligible_count=len(candidates), excluded_count=len(excluded), excluded=excluded,
                selected_count=len(dataset.selected_security_ids), original_target_count=dataset.original_target_count,
                subset_only=True, historical_universe_verified=False,
                quality_gaps=list(dataset.quality_gaps) + ['subset_selection_bias', 'historical_universe_unverified'])
