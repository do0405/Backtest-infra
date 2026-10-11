from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest

from backtest_data.prices.trade_aggregation import TradeEvent, aggregate_trades
from backtest_data.prices.vwap import daily_vwap
from test_corporate_actions import at
from test_price_domain import context


def trade(**changes):
    value = TradeEvent("e1", "t1", context(), date(2024, 1, 3), "trade", 1, D("100"), D("10"), True,
                       "regular_condition_v1", "c1", "hash1", at(4), at(4))
    return replace(value, **changes)


def aggregate(events, **changes):
    return aggregate_trades(events, context=context(), session=date(2024, 1, 3), as_of=at(6),
                            trade_scope="regular_condition_v1", source_contract_id="c1", raw_ref="aggregate_hash",
                            materialized_at=at(8), complete_evidence="full verified archive page chain",
                            completeness_available_at=at(4), completeness_captured_at=at(4), **changes)


def test_deduplicate_ids_and_apply_only_visible_corrections():
    correction = trade(event_id="e2", event_type="correct", sequence=2, price=D("120"), available_at=at(7), captured_at=at(7))
    result = aggregate([trade(), trade(), correction])
    assert result.sum_price_volume == D("1000")
    assert result.eligible_volume == D("10")
    result = aggregate([trade(), replace(correction, available_at=at(5))])
    assert result.sum_price_volume == D("1200")


def test_cancelled_trade_and_ineligible_conditions_are_removed_from_matched_scope():
    cancel = trade(event_id="e2", event_type="cancel", sequence=2, price=None, quantity=None, available_at=at(5))
    ineligible = trade(event_id="e3", trade_id="t2", eligible=False, price=D("50"), quantity=D("20"))
    result = aggregate([trade(), cancel, ineligible])
    assert result.sum_price_volume == 0
    assert result.eligible_volume == 0
    assert result.no_trade_evidence


def test_conflicting_id_or_unresolved_correction_cannot_be_aggregated():
    with pytest.raises(ValueError, match="conflicting"):
        aggregate([trade(), trade(price=D("101"))])
    with pytest.raises(ValueError, match="unresolved"):
        aggregate([trade(event_type="correct", sequence=2)])


def test_incompatible_trade_inputs_are_rejected_and_partial_archive_not_certified():
    with pytest.raises(ValueError, match="context"):
        aggregate([trade(context=context(feed="iex"))])
    with pytest.raises(ValueError, match="complete"):
        aggregate_trades([], context=context(), session=date(2024, 1, 3), as_of=at(6), trade_scope="regular_condition_v1",
                         source_contract_id="c1", raw_ref="hash", materialized_at=at(6), complete_evidence=None)


def test_observed_mode_requires_event_capture_and_corrected_state_archive_is_labeled():
    correction = trade(event_id="e2", event_type="correct", sequence=2, price=D("120"), available_at=at(5), captured_at=at(7))
    result = aggregate([trade(), correction], history_mode="observed_system")
    assert result.sum_price_volume == 1000
    result = aggregate([trade()], archive_mode="corrected_state_only")
    assert result.historical_replay_supported is False
    with pytest.raises(ValueError, match="replay"):
        aggregate([trade()], archive_mode="corrected_state_only", history_mode="observed_system")


def test_materialized_time_cannot_precede_capture_of_used_input():
    with pytest.raises(ValueError, match="materialized"):
        aggregate_trades([trade(captured_at=at(7))], context=context(), session=date(2024, 1, 3), as_of=at(6),
                         trade_scope="regular_condition_v1", source_contract_id="c1", raw_ref="hash", materialized_at=at(6),
                         complete_evidence="full session archive", completeness_available_at=at(4), completeness_captured_at=at(4))


def test_unknown_eligible_trade_availability_cannot_be_certified_as_no_trade():
    result = aggregate([trade(available_at=None)])
    assert result.sum_price_volume is None
    assert result.eligible_volume is None
    assert result.no_trade_evidence is None
    assert daily_vwap([result], session=result.session, as_of=at(9)).value is None
    assert daily_vwap([result], session=result.session, as_of=at(9)).reason == "unknown_trade_availability"


def test_unknown_correction_availability_cannot_be_ignored_for_exact_total():
    unknown = trade(event_id="e2", event_type="correct", sequence=2, price=D("120"), available_at=None)
    result = aggregate([trade(), unknown])
    assert result.sum_price_volume is None
    assert result.eligible_volume is None


def test_archive_completeness_time_prevents_intraday_replay_of_full_day():
    morning = datetime(2024, 1, 3, 10, tzinfo=timezone.utc)
    cutoff = datetime(2024, 1, 3, 12, tzinfo=timezone.utc)
    complete = datetime(2024, 1, 3, 17, 15, tzinfo=timezone.utc)
    result = aggregate_trades([trade(available_at=morning, captured_at=morning)], context=context(), session=date(2024, 1, 3),
                             as_of=at(6), trade_scope="regular_condition_v1", source_contract_id="c1", raw_ref="hash",
                             materialized_at=at(8), complete_evidence="session complete with verified 15 minute source delay",
                             completeness_available_at=complete, completeness_captured_at=at(4))
    assert result.available_at == complete
    assert result.captured_at == at(4)
    assert daily_vwap([result], session=result.session, as_of=cutoff).value is None


def test_archive_without_completeness_availability_or_before_completion_is_unavailable():
    with pytest.raises(ValueError, match="completeness"):
        aggregate_trades([trade()], context=context(), session=date(2024, 1, 3), as_of=at(6), trade_scope="regular_condition_v1",
                         source_contract_id="c1", raw_ref="hash", materialized_at=at(8), complete_evidence="full archive",
                         completeness_available_at=None, completeness_captured_at=at(4))
    with pytest.raises(ValueError, match="completeness"):
        aggregate_trades([trade()], context=context(), session=date(2024, 1, 3), as_of=at(6), trade_scope="regular_condition_v1",
                         source_contract_id="c1", raw_ref="hash", materialized_at=at(8), complete_evidence="full archive",
                         completeness_available_at=at(7), completeness_captured_at=at(4))


def test_empty_verified_archive_uses_completion_time_for_zero_evidence():
    result = aggregate([])
    assert result.available_at == at(4)
    assert result.captured_at == at(4)
    assert result.eligible_volume == 0
    assert result.no_trade_evidence


def test_observed_aggregation_requires_completeness_capture_at_cutoff():
    with pytest.raises(ValueError, match="completeness"):
        aggregate_trades([trade()], context=context(), session=date(2024, 1, 3), as_of=at(6), trade_scope="regular_condition_v1",
                         source_contract_id="c1", raw_ref="hash", materialized_at=at(8), complete_evidence="full archive",
                         completeness_available_at=at(4), completeness_captured_at=at(7), history_mode="observed_system")
