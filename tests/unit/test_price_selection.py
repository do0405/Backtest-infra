from dataclasses import replace
from decimal import Decimal as D

from backtest_data.prices.selection import select_observation
from backtest_data.prices.validation import ComparisonContract, compare_observations
from test_corporate_actions import at
from test_price_domain import context, observation


def contract(**changes):
    return replace(ComparisonContract("tick table v1", tick_size=D("0.01"), quantity_tolerance=D("0"), independent_paths_verified=True), **changes)


def independent_observation(**changes):
    return observation(**(dict(id="p2", source_contract_id="c2", upstream_id="upstream2", processing_path="path2") | changes))


def test_only_equivalent_contracts_are_compared():
    for ctx in [context(feed="iex"), context(session_scope="extended"), context(quantity_unit="lots"), context(basis="split_adjusted", share_basis="2024")]:
        comparison = compare_observations(observation(), independent_observation(context=ctx), contract=contract())
        assert comparison.status == "incomparable"


def test_reported_precision_or_real_tick_is_required_for_price_tolerance():
    left = observation(upstream_id="upstream1", processing_path="path1")
    right = independent_observation(close=D("105.01"))
    assert compare_observations(left, right, contract=contract()).status == "agree"
    assert compare_observations(left, right, contract=contract(tick_size=None)).status == "unresolved"
    assert compare_observations(left, independent_observation(close=D("105.02")), contract=contract()).status == "mismatch"


def test_same_wrapper_upstream_is_not_independent_and_missing_is_not_suspension():
    left = observation(upstream_id="same", processing_path="same")
    right = observation(id="p2", source_contract_id="c2", upstream_id="same", processing_path="same")
    comparison = compare_observations(left, right, contract=contract())
    assert comparison.independent is False
    missing = compare_observations(left, replace(right, volume=None), contract=contract())
    assert "missing_quantity" in {issue.code for issue in missing.issues}


def test_unresolved_mismatch_is_quarantined_without_majority_or_spike_deletion():
    left = observation(upstream_id="upstream1", processing_path="path1")
    right = independent_observation(close=D("106"))
    comparison = compare_observations(left, right, contract=contract())
    decision = select_observation(left, source_contract_verified=True, recorded_at=at(6), rule_version="price_selection_v1",
                                  comparisons=[comparison], validation_issue_ids=["issue1"], decision_id="decision1")
    assert decision.status == "quarantined"
    assert decision.observation_ids == ("p1", "p2")
    assert decision.validation_issue_ids == ("issue1",)
    assert left.close == 105
    assert right.close == 106


def test_contract_and_independent_evidence_grades_remain_distinct():
    left = observation(upstream_id="upstream1", processing_path="path1")
    single = select_observation(left, source_contract_verified=True, recorded_at=at(6), rule_version="v1", decision_id="d1")
    assert single.status == "accepted"
    assert single.evidence_grade == "contract_verified"
    comparison = compare_observations(left, independent_observation(), contract=contract())
    checked = select_observation(left, source_contract_verified=True, recorded_at=at(6), rule_version="v1", comparisons=[comparison], decision_id="d2")
    assert checked.evidence_grade == "independent_crosschecked"
    assert checked.recorded_at == at(6)
    assert select_observation(left, source_contract_verified=False, recorded_at=at(6), rule_version="v1", decision_id="d3").status == "provisional"


def test_selection_does_not_adopt_unknown_availability():
    candidate = observation(available_at=None)
    decision = select_observation(candidate, source_contract_verified=True, recorded_at=at(6), rule_version="v1", decision_id="d1")
    assert decision.status == "quarantined"
    assert decision.reason == "availability_unknown"


def test_incomplete_quantity_cannot_be_selected_as_core_price_volume():
    candidate = observation(volume=None)
    decision = select_observation(candidate, source_contract_verified=True, recorded_at=at(6), rule_version="v1", decision_id="d1")
    assert decision.status == "quarantined"
    assert decision.reason == "required_quantity_missing"


def test_verified_shared_sip_can_be_independent_only_with_distinct_processing_paths():
    left = observation(upstream_id="SIP", processing_path="collector1")
    right = independent_observation(upstream_id="SIP")
    assert compare_observations(left, right, contract=contract()).independent is True
