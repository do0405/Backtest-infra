from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from backtest_data.prices.vwap import Anchor, DailyVWAPStat, anchored_vwap, daily_vwap, exact_vwap, rolling_vwap
from test_corporate_actions import action, at, coverage
from test_price_domain import context


def stat(day, numerator="1000", denominator="10", **changes):
    fields = dict(id=f"v{day}", context=context(), session=date(2024, 1, day), sum_price_volume=D(numerator) if numerator is not None else None,
                  eligible_volume=D(denominator) if denominator is not None else None, trade_scope="eligible_regular_trades", method="matched_daily_statistics",
                  source_contract_id="c1", raw_ref="hash1", revision=1, available_at=at(day+1), captured_at=at(day+1), materialized_at=at(day+1))
    return DailyVWAPStat(**(fields | changes))


def test_exact_period_vwap_uses_matched_sum_not_daily_average():
    result = exact_vwap([stat(3), stat(4, "2000", "40")], sessions=[date(2024, 1, 3), date(2024, 1, 4)], as_of=at(6), action_coverage=coverage())
    assert result.value == D("60")
    assert result.sum_price_volume == D("3000")
    assert result.eligible_volume == D("50")
    assert result.input_ids == ("v3", "v4")


def test_split_changes_denominator_and_preserves_numerator():
    result = exact_vwap([stat(3, "10000", "100"), stat(4, "12000", "200")], sessions=[date(2024, 1, 3), date(2024, 1, 4)],
                        as_of=at(6), actions=[action()], basis_session=date(2024, 1, 4), action_coverage=coverage())
    assert result.value == D("55")
    assert result.sum_price_volume == D("22000")
    assert result.eligible_volume == D("400")
    assert result.action_ids == ("a1",)


def test_rolling_uses_calendar_sessions_and_does_not_compress_missing():
    grid = [date(2024, 1, 1) + timedelta(days=i) for i in range(5)]
    result = rolling_vwap([stat(1), stat(3), stat(4), stat(5)], session_grid=grid, end_session=grid[3], window=3, as_of=at(7))
    assert result.value is None
    assert result.sessions == tuple(grid[1:4])
    assert result.missing_sessions == (grid[1],)
    assert result.reason == "missing_sessions"


def test_twenty_session_window_reports_insufficient_grid():
    result = rolling_vwap([stat(3)], session_grid=[date(2024, 1, 3)], end_session=date(2024, 1, 3), window=20, as_of=at(6))
    assert result.value is None
    assert result.reason == "insufficient_session_grid"


def test_anchor_must_be_known_at_cutoff_and_on_grid():
    grid = [date(2024, 1, 3), date(2024, 1, 4)]
    anchor = Anchor(grid[0], at(7), "event-confirmed")
    assert anchored_vwap([stat(3), stat(4)], session_grid=grid, end_session=grid[-1], anchor=anchor, as_of=at(6)).reason == "anchor_not_known"
    anchor = replace(anchor, known_at=at(2))
    assert anchored_vwap([stat(3), stat(4)], session_grid=grid, end_session=grid[-1], anchor=anchor, as_of=at(6), action_coverage=coverage()).value == 100


def test_missing_stat_is_not_zero_and_evidenced_zero_returns_null_denominator():
    assert daily_vwap([stat(3, None, None)], session=date(2024, 1, 3), as_of=at(6)).reason == "missing_statistics"
    zero = stat(3, "0", "0", no_trade_evidence="provider:no eligible trades")
    assert daily_vwap([zero], session=zero.session, as_of=at(6)).reason == "zero_eligible_volume"
    with pytest.raises(ValueError, match="evidence"):
        stat(3, "0", "0")


@pytest.mark.parametrize("changes", [
    {"trade_scope": "extended"},
    {"context": context(feed="iex")},
    {"context": context(feed="")},
    {"context": context(quantity_unit="lots")},
    {"context": context(basis="total_return", share_basis="gross")},
    {"source_contract_id": "c2"},
])
def test_incompatible_statistics_never_silently_mix(changes):
    result = exact_vwap([stat(3), stat(4, **changes)], sessions=[date(2024, 1, 3), date(2024, 1, 4)], as_of=at(6))
    assert result.value is None
    assert result.reason == "incompatible_statistics"


@pytest.mark.parametrize("method", ["provider_reported", "bar_proxy"])
def test_provider_vwap_times_general_volume_is_not_exact(method):
    result = daily_vwap([stat(3, method=method)], session=date(2024, 1, 3), as_of=at(6))
    assert result.value is None
    assert result.reason == "method_not_exact"


def test_revised_stat_visibility_and_observed_materialization_gates():
    original = stat(3, decision_recorded_at=at(4))
    revised = stat(3, "2000", "10", id="v3r2", revision=2, available_at=at(7), captured_at=at(7), materialized_at=at(7), decision_recorded_at=at(7))
    assert daily_vwap([original, revised], session=original.session, as_of=at(6)).value == 100
    assert daily_vwap([original, revised], session=original.session, as_of=at(8)).value == 200
    assert daily_vwap([replace(original, materialized_at=at(7))], session=original.session, as_of=at(6), history_mode="observed_system").reason == "missing_sessions"
    assert daily_vwap([replace(original, decision_recorded_at=None)], session=original.session, as_of=at(6), history_mode="observed_system").reason == "missing_sessions"
    assert daily_vwap([original], session=original.session, as_of=at(6), history_mode="observed_system").value == 100


def test_corrected_state_archive_cannot_claim_observed_replay():
    value = stat(3, historical_replay_supported=False, decision_recorded_at=at(4))
    assert daily_vwap([value], session=value.session, as_of=at(6), history_mode="observed_system").reason == "historical_replay_unsupported"
    assert daily_vwap([value], session=value.session, as_of=at(6)).value == 100


def test_twenty_session_window_keeps_gap_even_when_twenty_observed_rows_exist():
    grid = [date(2024, 1, day) for day in range(1, 26)]
    observations = [stat(day) for day in range(1, 26) if day != 10]
    result = rolling_vwap(observations, session_grid=grid, end_session=grid[-1], window=20, as_of=at(27))
    assert len(result.sessions) == 20
    assert result.sessions[0] == date(2024, 1, 6)
    assert result.missing_sessions == (date(2024, 1, 10),)


def test_other_source_copy_of_same_session_requires_explicit_selection():
    first = stat(3)
    other = replace(first, id="v3c2", source_contract_id="c2")
    result = daily_vwap([first, other], session=first.session, as_of=at(6), compatible_source_contracts=frozenset({"c1", "c2"}))
    assert result.reason == "ambiguous_session_selection"


def test_adjusted_statistics_need_exact_query_share_basis():
    adjusted = stat(3, context=context(basis="split_adjusted", share_basis="as_of:2024-01-03"))
    assert daily_vwap([adjusted], session=adjusted.session, basis_session=date(2024, 1, 4), as_of=at(6)).reason == "incompatible_statistics"


def test_unsupported_complex_action_blocks_only_affected_exact_window():
    complex_event = action(action_type="rights", split_ratio=None)
    first = stat(3)
    assert daily_vwap([first], session=first.session, as_of=at(6), actions=[complex_event]).value == 100
    result = exact_vwap([first, stat(4)], sessions=[first.session, date(2024, 1, 4)], as_of=at(6), actions=[complex_event], action_coverage=coverage())
    assert result.reason == "unsupported_action"


def test_single_stat_also_requires_explicit_feed():
    value = stat(3, context=context(feed=""))
    assert daily_vwap([value], session=value.session, as_of=at(6)).reason == "incompatible_statistics"


def test_cross_session_vwap_cannot_assume_empty_actions_means_no_split():
    result = exact_vwap([stat(3), stat(4)], sessions=[date(2024, 1, 3), date(2024, 1, 4)], as_of=at(6), actions=[])
    assert result.value is None
    assert result.reason == "action_coverage_unavailable"


def test_explicitly_adjusted_single_session_also_requires_action_coverage():
    value = stat(3, context=context(basis="split_adjusted", share_basis="as_of:2024-01-03"))
    result = daily_vwap([value], session=value.session, as_of=at(6))
    assert result.value is None
    assert result.reason == "action_coverage_unavailable"


def test_split_vwap_requires_coverage_for_entire_interval_and_known_source():
    statistics = [stat(3), stat(4)]
    sessions = [s.session for s in statistics]
    for evidence in [coverage(verified=False), coverage(through_session=date(2024, 1, 3)), coverage(available_at=at(7)), coverage(source_contract_id="other")]:
        result = exact_vwap(statistics, sessions=sessions, as_of=at(6), actions=[action()], action_coverage=evidence)
        assert result.reason == "action_coverage_unavailable"


def test_raw_daily_vwap_stays_available_without_split_derivation():
    value = stat(3)
    assert daily_vwap([value], session=value.session, as_of=at(6)).value == 100
    assert daily_vwap([value], session=value.session, as_of=at(6), basis_session=value.session).reason == "action_coverage_unavailable"


def test_observed_period_vwap_requires_historical_coverage_decision():
    statistics = [stat(3, decision_recorded_at=at(4)), stat(4, decision_recorded_at=at(5))]
    sessions = [s.session for s in statistics]
    for evidence in [coverage(captured_at=at(7)), coverage(decision_recorded_at=None), coverage(decision_recorded_at=at(7))]:
        result = exact_vwap(statistics, sessions=sessions, as_of=at(6), history_mode="observed_system", action_coverage=evidence)
        assert result.reason == "action_coverage_unavailable"
    assert exact_vwap(statistics, sessions=sessions, as_of=at(6), history_mode="observed_system", action_coverage=coverage()).value == 100
