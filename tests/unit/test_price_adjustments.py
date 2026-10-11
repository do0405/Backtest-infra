from dataclasses import replace
from datetime import date
from decimal import Decimal as D

from backtest_data.prices.adjustments import split_adjust, split_factor, total_return_benchmark
import pytest
from test_corporate_actions import action, at, coverage
from test_price_domain import observation


def bar(day, price, volume="100"):
    return observation(id=f"p{day}", session=date(2024, 1, day), open=D(price), high=D(price), low=D(price), close=D(price), volume=D(volume), notional=D(price)*D(volume))


def test_split_price_quantity_and_notional_transform_together():
    adjusted = split_adjust(bar(3, "100"), [action()], as_of=at(5), basis_session=date(2024, 1, 5), action_coverage=coverage())
    assert adjusted.observation.close == D("50")
    assert adjusted.observation.volume == D("200")
    assert adjusted.observation.notional == D("10000")
    assert adjusted.factor == D("0.5")
    assert adjusted.action_ids == ("a1",)


def test_reverse_split_and_future_information_are_blocked():
    reverse = action(split_ratio=D("0.1"))
    assert split_adjust(bar(3, "100"), [reverse], as_of=at(5), basis_session=date(2024, 1, 5), action_coverage=coverage()).observation.close == D("1000")
    assert split_adjust(bar(3, "100"), [action(available_at=at(6))], as_of=at(5), basis_session=date(2024, 1, 5), action_coverage=coverage()).factor == 1


def test_dividend_gross_reinvestment_is_separate_from_trading_prices():
    dividend = action(action_type="cash_dividend", split_ratio=None, cash_amount=D("10"), currency="USD", cash_share_basis="pre_event_share")
    result = total_return_benchmark([bar(3, "100"), bar(4, "90"), bar(5, "99")], [dividend], as_of=at(6), action_coverage=coverage())
    assert [p.value for p in result.points] == [D("100"), D("100"), D("110")]
    assert result.policy == "gross_ex_date_reinvestment_v1"
    assert split_adjust(bar(3, "100"), [dividend], as_of=at(6), basis_session=date(2024, 1, 5), action_coverage=coverage()).observation.close == 100


def test_unsupported_complex_event_makes_benchmark_unavailable():
    event = action(action_type="spinoff", split_ratio=None)
    result = total_return_benchmark([bar(3, "100"), bar(4, "90"), bar(5, "99")], [event], as_of=at(6), action_coverage=coverage())
    assert result.points[0].value == 100
    assert result.points[1].value is None
    assert "unsupported_action" in result.points[1].reason
    assert result.points[2].value is None


def test_unverified_dividend_share_basis_is_not_assumed():
    dividend = action(action_type="cash_dividend", split_ratio=None, cash_amount=D("10"), currency="USD")
    result = total_return_benchmark([bar(3, "100"), bar(4, "90")], [dividend], as_of=at(6), action_coverage=coverage())
    assert result.points[1].value is None
    assert result.points[1].reason == "incompatible_dividend_basis"


def test_split_derived_series_does_not_invent_complex_event_transform():
    event = action(action_type="merger", split_ratio=None)
    original = bar(3, "100")
    result = split_adjust(original, [event], as_of=at(6), basis_session=date(2024, 1, 5), action_coverage=coverage())
    assert result.observation is None
    assert result.factor is None
    assert result.reason == "unsupported_action"
    assert result.action_ids == ("a1",)
    assert original.close == 100


def test_empty_actions_are_not_verified_no_event_coverage():
    original = bar(3, "100")
    result = split_adjust(original, [], as_of=at(6), basis_session=date(2024, 1, 5))
    assert result.observation is None
    assert result.factor is None
    assert result.reason == "action_coverage_unavailable"
    assert original.close == 100


def test_verified_no_event_coverage_certifies_factor_one():
    result = split_adjust(bar(3, "100"), [], as_of=at(6), basis_session=date(2024, 1, 5), action_coverage=coverage())
    assert result.factor == 1
    assert result.observation.close == 100
    assert result.action_coverage_ref == "verified complete corporate-action/no-event ledger"


def test_incomplete_or_invisible_action_coverage_is_unavailable():
    for evidence in [coverage(verified=False), coverage(security_id="other"), coverage(through_session=date(2024, 1, 4)),
                     coverage(from_session=date(2024, 1, 4)), coverage(available_at=None), coverage(available_at=at(7))]:
        result = split_adjust(bar(3, "100"), [], as_of=at(6), basis_session=date(2024, 1, 5), action_coverage=evidence)
        assert result.observation is None
        assert result.reason == "action_coverage_unavailable"


def test_observed_split_coverage_requires_capture_and_decision_at_cutoff():
    original = replace(bar(3, "100"), captured_at=at(4), decision_recorded_at=at(4))
    for evidence in [coverage(captured_at=at(7)), coverage(decision_recorded_at=at(7)), coverage(decision_recorded_at=None)]:
        result = split_adjust(original, [], as_of=at(6), basis_session=date(2024, 1, 5), history_mode="observed_system", action_coverage=evidence)
        assert result.reason == "action_coverage_unavailable"


def test_split_factor_itself_cannot_bypass_coverage_gate():
    with pytest.raises(ValueError, match="action_coverage_unavailable"):
        split_factor(security_id="s1", session=date(2024, 1, 3), basis_session=date(2024, 1, 5), actions=[], as_of=at(6))


def test_unknown_action_availability_prevents_verified_no_event_assumption():
    result = split_adjust(bar(3, "100"), [action(available_at=None)], as_of=at(6), basis_session=date(2024, 1, 5), action_coverage=coverage())
    assert result.reason == "action_coverage_unavailable"


def test_adjusted_observation_includes_coverage_evidence_visibility():
    result = split_adjust(bar(3, "100"), [], as_of=at(6), basis_session=date(2024, 1, 5),
                          action_coverage=coverage(available_at=at(6), captured_at=at(7)))
    assert result.observation.available_at == at(6)
    assert result.observation.captured_at == at(7)


def test_gross_benchmark_without_action_coverage_cannot_assume_no_dividend():
    originals = [bar(3, "100"), bar(4, "90")]
    result = total_return_benchmark(originals, [], as_of=at(6))
    assert [point.value for point in result.points] == [None, None]
    assert result.reason == "action_coverage_unavailable"
    assert all(point.reason == "action_coverage_unavailable" for point in result.points)
    assert [original.close for original in originals] == [D("100"), D("90")]


def test_gross_benchmark_verified_no_event_coverage_allows_actual_price_return():
    result = total_return_benchmark([bar(3, "100"), bar(4, "90")], [], as_of=at(6), action_coverage=coverage())
    assert [point.value for point in result.points] == [D("100"), D("90")]
    assert result.reason is None
    assert result.action_coverage_ref == "verified complete corporate-action/no-event ledger"


def test_gross_benchmark_requires_coverage_for_full_interval():
    originals = [bar(3, "100"), bar(4, "90")]
    for evidence in [coverage(from_session=date(2024, 1, 4)), coverage(through_session=date(2024, 1, 3)),
                     coverage(verified=False), coverage(security_id="other"), coverage(available_at=at(7))]:
        result = total_return_benchmark(originals, [], as_of=at(6), action_coverage=evidence)
        assert result.reason == "action_coverage_unavailable"
        assert all(point.value is None for point in result.points)


def test_observed_gross_benchmark_cannot_use_later_coverage_adoption():
    originals = [replace(bar(3, "100"), decision_recorded_at=at(5)), replace(bar(4, "90"), decision_recorded_at=at(5))]
    for evidence in [coverage(decision_recorded_at=at(7)), coverage(decision_recorded_at=None), coverage(captured_at=at(7))]:
        result = total_return_benchmark(originals, [], as_of=at(6), history_mode="observed_system", action_coverage=evidence)
        assert result.reason == "action_coverage_unavailable"
        assert all(point.value is None for point in result.points)
    result = total_return_benchmark(originals, [], as_of=at(6), history_mode="observed_system", action_coverage=coverage(decision_recorded_at=at(6)))
    assert [point.value for point in result.points] == [D("100"), D("90")]


def test_gross_benchmark_coverage_does_not_ignore_unknown_dividend_availability():
    dividend = action(action_type="cash_dividend", split_ratio=None, cash_amount=D("10"), currency="USD",
                      cash_share_basis="pre_event_share", available_at=None)
    result = total_return_benchmark([bar(3, "100"), bar(4, "90")], [dividend], as_of=at(6), action_coverage=coverage())
    assert result.reason == "action_coverage_unavailable"
    assert all(point.value is None for point in result.points)


def test_gross_benchmark_with_coverage_keeps_pure_split_return_constant():
    originals = [bar(3, "100"), bar(4, "50", "200")]
    result = total_return_benchmark(originals, [action()], as_of=at(6), action_coverage=coverage())
    assert [point.value for point in result.points] == [D("100"), D("100")]
    assert [original.close for original in originals] == [D("100"), D("50")]
    assert [original.volume for original in originals] == [D("100"), D("200")]


def test_gross_dividend_uses_ex_date_even_when_effective_and_payment_are_future():
    dividend = action(action_type="cash_dividend", split_ratio=None, cash_amount=D("10"), currency="USD",
                      cash_share_basis="pre_event_share", effective_date=date(2024, 1, 20), pay_date=date(2024, 1, 20))
    result = total_return_benchmark([bar(3, "100"), bar(4, "90")], [dividend], as_of=at(6), action_coverage=coverage(through_session=date(2024, 1, 4)))
    assert [point.value for point in result.points] == [D("100"), D("100")]
    assert result.points[1].action_ids == ("a1",)


def test_unknown_dividend_availability_is_checked_by_ex_date_for_coverage():
    dividend = action(action_type="cash_dividend", split_ratio=None, cash_amount=D("10"), currency="USD",
                      cash_share_basis="pre_event_share", effective_date=date(2024, 1, 20), available_at=None)
    result = total_return_benchmark([bar(3, "100"), bar(4, "90")], [dividend], as_of=at(6), action_coverage=coverage())
    assert result.reason == "action_coverage_unavailable"
    assert all(point.value is None for point in result.points)


def test_gross_benchmark_rejects_lot_prices_with_per_share_cash():
    from test_price_domain import context

    lot_context = context(price_unit="currency_per_lot", quantity_unit="lots")
    values = [replace(bar(3, "1000"), context=lot_context), replace(bar(4, "900"), context=lot_context)]
    dividend = action(action_type="cash_dividend", split_ratio=None, cash_amount=D("10"), currency="USD", cash_share_basis="pre_event_share")
    result = total_return_benchmark(values, [dividend], as_of=at(6), action_coverage=coverage())
    assert result.reason == "unsupported_benchmark_units"
    assert all(point.value is None for point in result.points)
    assert values[0].close == 1000


def test_gross_ex_date_policy_does_not_make_future_split_effective_early():
    future_split = action(effective_date=date(2024, 1, 20), ex_date=date(2024, 1, 4))
    result = total_return_benchmark([bar(3, "100"), bar(4, "90")], [future_split], as_of=at(6), action_coverage=coverage())
    assert [point.value for point in result.points] == [D("100"), D("90")]
    assert result.points[1].action_ids == ()
