from dataclasses import replace
from datetime import date
from decimal import Context, Decimal as D, Inexact, ROUND_UP, Rounded, localcontext

import pytest

from backtest_data.prices.adjustments import split_adjust, total_return_benchmark
from backtest_data.prices.vwap import daily_vwap, exact_vwap
from test_corporate_actions import action, at, coverage
from test_price_adjustments import bar
from test_trade_aggregation import aggregate, trade
from test_vwap import stat


POLICY = "price_decimal50_half_even_v1"


def hostile_context():
    ctx = Context(prec=4, rounding=ROUND_UP)
    ctx.traps[Inexact] = True
    ctx.traps[Rounded] = True
    return ctx


def test_exact_trade_product_and_vwap_ignore_caller_precision_and_traps():
    value = trade(price=D("100.01"), quantity=D("10.01"))
    with localcontext(hostile_context()):
        result = aggregate([value])
        vwap = daily_vwap([result], session=value.session, as_of=at(9))
    assert result.sum_price_volume == D("1001.1001")
    assert result.eligible_volume == D("10.01")
    assert vwap.value == D("100.01")
    assert result.numeric_policy == POLICY
    assert vwap.numeric_policy == POLICY
    assert result.input_signature.startswith(POLICY + ":")


def test_trade_sum_preserves_large_coefficient_plus_one_at_default_precision():
    first = trade(price=D("1e28"), quantity=D("1"))
    second = trade(event_id="e2", trade_id="t2", price=D("1"), quantity=D("1"))
    with localcontext(Context(prec=28)):
        result = aggregate([first, second])
    assert result.sum_price_volume == D("10000000000000000000000000001")


def test_rolling_numerator_and_denominator_are_both_exact_before_division():
    values = [stat(3, "1e28", "1e28"), stat(4, "1", "1")]
    with localcontext(hostile_context()):
        result = exact_vwap(values, sessions=[v.session for v in values], as_of=at(6), action_coverage=coverage())
    assert result.sum_price_volume == D("10000000000000000000000000001")
    assert result.eligible_volume == D("10000000000000000000000000001")
    assert result.value == 1
    assert result.input_signature.startswith(POLICY + ":")


def test_split_uses_exact_quantity_factor_instead_of_dividing_by_rounded_inverse():
    value = bar(3, "100")
    with localcontext(hostile_context()):
        result = split_adjust(value, [action(split_ratio=D("3"))], as_of=at(6), basis_session=date(2024, 1, 5), action_coverage=coverage())
    assert result.observation.close == D("33." + "3" * 48)
    assert result.observation.volume == D("300")
    assert result.observation.notional == D("10000")
    assert result.quantity_factor == 3
    assert result.numeric_policy == POLICY


def test_split_vwap_denominator_uses_exact_ratio_and_stable_fixed_division():
    values = [stat(3, "10000", "100"), stat(4, "12000", "200")]
    with localcontext(hostile_context()):
        result = exact_vwap(values, sessions=[v.session for v in values], as_of=at(6), actions=[action(split_ratio=D("3"))], action_coverage=coverage())
    assert result.sum_price_volume == 22000
    assert result.eligible_volume == 500
    assert result.value == 44


def test_gross_benchmark_rounding_is_fixed_even_with_hostile_caller_context():
    values = [bar(3, "100"), bar(4, "90")]
    with localcontext(hostile_context()):
        result = total_return_benchmark(values, [], as_of=at(6), base_value=D("100.12345"), action_coverage=coverage())
    assert [point.value for point in result.points] == [D("100.12345"), D("90.111105")]
    assert result.numeric_policy == POLICY
    assert result.input_signature.startswith(POLICY + ":")


def test_price_numeric_policy_has_an_explicit_supported_envelope():
    from backtest_data.prices.numeric import SOURCE_NUMERIC_POLICY, exact_multiply, exact_sum

    assert SOURCE_NUMERIC_POLICY.id == POLICY
    assert SOURCE_NUMERIC_POLICY.division_precision == 50
    assert SOURCE_NUMERIC_POLICY.rounding == "ROUND_HALF_EVEN"
    with pytest.raises(ValueError, match="numeric_envelope"):
        exact_sum([D("1e10001"), D("1")])
    with pytest.raises(ValueError, match="numeric_envelope"):
        exact_multiply(D("1e10000"), D("10"))


def test_numeric_policy_signature_changes_when_exact_inputs_change():
    first = aggregate([trade(price=D("100.01"), quantity=D("10.01"))])
    second = aggregate([trade(price=D("100.02"), quantity=D("10.01"))])
    assert first.input_signature != second.input_signature


def test_exact_query_rejects_unsupported_stat_numeric_policy():
    value = replace(stat(3), numeric_policy="unrecognized_policy")
    assert daily_vwap([value], session=value.session, as_of=at(6)).reason == "unsupported_numeric_policy"


def test_fixed_rounding_uses_half_even_at_the_fiftieth_significant_digit():
    from backtest_data.prices.numeric import fixed_divide, fixed_round

    with localcontext(hostile_context()):
        even = fixed_round(D("1." + "0" * 48 + "25"))
        odd = fixed_round(D("1." + "0" * 48 + "35"))
        third = fixed_divide(D("1"), D("3"))
    assert even == D("1." + "0" * 48 + "2")
    assert odd == D("1." + "0" * 48 + "4")
    assert third == D("0." + "3" * 50)


def test_single_exact_large_exponent_does_not_round_zero_alignment():
    from backtest_data.prices.numeric import exact_sum

    with localcontext(hostile_context()):
        assert exact_sum([D("1e10000")]) == D("1e10000")


def test_split_quantity_preserves_large_coefficient_plus_one():
    quantity = D("10000000000000000000000000001")
    notional = D("1000000000000000000000000000100")
    value = replace(bar(3, "100"), volume=quantity, notional=notional)
    with localcontext(hostile_context()):
        result = split_adjust(value, [action(split_ratio=D("3"))], as_of=at(6), basis_session=date(2024, 1, 5), action_coverage=coverage())
    assert result.observation.volume == D("30000000000000000000000000003")
    assert result.observation.notional == notional


def test_numeric_signatures_are_stable_across_input_order_and_caller_context():
    first = trade(price=D("100.01"), quantity=D("10.01"))
    second = trade(event_id="e2", trade_id="t2", price=D("1"), quantity=D("1"))
    normal = aggregate([first, second])
    with localcontext(hostile_context()):
        reordered = aggregate([second, first])
    assert normal.input_signature == reordered.input_signature
    assert normal.id == reordered.id


def test_caller_exponent_limits_and_flags_are_not_used_or_modified():
    value = trade(price=D("100.01"), quantity=D("10.01"))
    hostile = hostile_context()
    hostile.Emax = 1
    hostile.Emin = -1
    with localcontext(hostile) as caller:
        before = caller.copy()
        result = aggregate([value])
        assert daily_vwap([result], session=value.session, as_of=at(9)).value == D("100.01")
        assert caller.flags == before.flags
        assert caller.prec == before.prec
        assert caller.traps == before.traps
