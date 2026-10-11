from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timezone
from decimal import Decimal as D, localcontext
import importlib

import pytest


CUTOFF = datetime(2026, 10, 7, 23, 59, 59, tzinfo=timezone.utc)
SESSION = date(2016, 1, 4)
OPEN = datetime(2016, 1, 4, 14, 30, tzinfo=timezone.utc)
CLOSE = datetime(2016, 1, 4, 21, tzinfo=timezone.utc)
CAPTURE = datetime(2026, 10, 10, tzinfo=timezone.utc)


def api():
    try:
        return importlib.import_module("backtest_data.supplemental.alpaca_inputs")
    except ModuleNotFoundError:
        pytest.fail("Alpaca input module has not been implemented")


def action(**changes):
    row = dict(id="split-1", symbol="AAPL", new_rate="4", old_rate="1",
               process_date="2020-08-28", ex_date="2020-08-31")
    return row | changes


def action_row(**changes):
    return api().normalize_corporate_action_page(
        {"corporate_actions": {"forward_splits": [action(**changes)]}, "next_page_token": None},
        cutoff=date(2026, 10, 7))[0]


def trade(**changes):
    return dict(i="1", x="Q", z="C", p="0.1000000000000000000000000001", s="3",
                t="2016-01-04T14:30:00.000000001Z", c=["@"]) | changes


def request(**changes):
    return api().trade_request("AAPL", SESSION, session_open=OPEN, session_close=CLOSE,
                               cutoff=CUTOFF, **changes)


def page(trades, *, token=None, next_token=None, symbol="AAPL", **changes):
    spec = request().with_page_token(token)
    return api().CapturedTradePage(spec, {"symbol": symbol, "trades": trades,
                                         "next_page_token": next_token}, "raw-1", CAPTURE, **changes)


def aggregate(pages):
    return api().aggregate_trade_pages(pages, symbol="AAPL", session=SESSION,
                                       session_open=OPEN, session_close=CLOSE, cutoff=CUTOFF)


def test_future_action_preserves_process_date_and_never_claims_effectiveness():
    row = action_row(process_date="2026-10-06", ex_date="2026-10-12")
    assert row["effective_date"] == date(2026, 10, 12)
    assert row["fields"]["process_date"] == "2026-10-06"
    assert row["fields"]["future_effective"] is True
    assert "future_effective" in row["issues"]
    assert row.get("published_date") is None
    assert row["fields"]["verification_status"] == "pending"


def test_unknown_condition_keeps_partial_candidate_but_denies_exact_qualification():
    result = aggregate([page([trade(), trade(i="2", c=["UNRECOGNIZED"])])])
    assert result.sum_price_volume == D("0.3000000000000000000000000003")
    assert result.qualified is False
    assert "unknown_trade_condition" in result.issues
    assert result.as_staged_row()["fields"]["full_vwap_certified"] is False


def test_duplicates_use_tape_exchange_and_id_and_are_idempotent():
    result = aggregate([page([trade(), trade(), trade(x="N"), trade(z="A")])])
    assert result.eligible_volume == D("9")
    assert result.duplicate_count == D("1")
    assert result.eligible_trade_count == D("3")
    assert result == aggregate([page([trade(), trade(), trade(x="N"), trade(z="A")])])


def test_exact_decimal_products_and_sums_ignore_ambient_context():
    with localcontext() as context:
        context.prec = 3
        result = aggregate([page([trade(), trade(i="2", p="10000000000000000000000000000", s="1")])])
    assert result.sum_price_volume == D("10000000000000000000000000000.3000000000000000000000000003")
    assert result.eligible_volume == D("4")


def test_source_float_is_rejected_instead_of_turning_binary_float_into_exact_input():
    with pytest.raises(ValueError, match="float"):
        aggregate([page([trade(p=0.1)])])


def test_requests_cover_closed_full_period_all_types_all_symbols():
    plans = api().corporate_action_requests()
    assert len(plans) == 11
    assert dict(plans[0].params)["start"] == "2016-01-01"
    assert dict(plans[-1].params)["end"] == "2026-10-07"
    assert all(dict(p.params)["data_quality"] == "all" for p in plans)
    assert all("symbols" not in dict(p.params) and "types" not in dict(p.params) for p in plans)
    with pytest.raises(FrozenInstanceError):
        plans[0].url = "changed"
    with pytest.raises(ValueError, match="cutoff"):
        api().corporate_action_requests(end=date(2026, 10, 8))


def test_trade_request_disables_automatic_identity_mapping_and_uses_calendar_bounds():
    params = dict(request().params)
    assert params["feed"] == "sip"
    assert params["asof"] == "-"
    assert params["sort"] == "asc"
    assert params["start"] == OPEN.isoformat()
    assert params["end"] == CLOSE.isoformat()
    assert request().with_page_token("next").anchor_key == request().anchor_key
    assert request().with_page_token("next").request_key != request().request_key


@pytest.mark.parametrize("pages,reason", [
    (lambda: [page([trade()], next_token="p2")], "missing_trade_pages"),
    (lambda: [page([trade()], token="p2")], "missing_initial_page"),
    (lambda: [page([trade()], next_token="p2"), page([trade(i="2")], token="wrong")], "page_chain_mismatch"),
    (lambda: [page([trade()], symbol="TSLA")], "response_symbol_mismatch"),
])
def test_incomplete_or_wrong_page_chain_cannot_qualify(pages, reason):
    result = aggregate(pages())
    assert not result.qualified
    assert reason in result.issues


def test_terminal_page_and_known_exclusions_are_required_and_matched():
    result = aggregate([page([trade()], next_token="p2"), page([trade(i="2", c=["I"])], token="p2")])
    assert result.page_chain_complete
    assert result.qualified
    assert result.eligible_volume == D("3")
    assert result.excluded_trade_count == D("1")
    assert result.trade_scope == "regular_sale_only_v1"


def test_conflicting_duplicate_denies_qualification_without_summing_twice():
    result = aggregate([page([trade(), trade(p="2")])])
    assert "conflicting_trade_identity" in result.issues
    assert not result.qualified
    assert result.eligible_volume == D("3")


def test_nanosecond_outside_session_and_new_york_date_are_not_truncated_into_scope():
    result = aggregate([page([trade(t="2016-01-04T21:00:00.000000001Z"),
                              trade(i="2", t="2016-01-05T01:00:00Z")])])
    assert result.eligible_volume == 0
    assert result.excluded_trade_count == 2


def test_day_not_closed_by_cutoff_never_qualifies():
    result = api().aggregate_trade_pages([page([trade()])], symbol="AAPL", session=SESSION,
                                        session_open=OPEN, session_close=CLOSE, cutoff=OPEN)
    assert "session_not_closed" in result.issues
    assert not result.qualified


def test_unknown_action_types_and_missing_ex_dates_stay_evidence():
    row = api().normalize_corporate_action_page({"corporate_actions": {
        "new_complex_actions": [{"id": "x", "old_symbol": "ABC", "process_date": "2020-01-01"}]}},
        cutoff=date(2026, 10, 7))[0]
    assert row["entity_key"] == "ABC"
    assert row["effective_date"] is None
    assert row["fields"]["raw_type"] == "new_complex_actions"
    assert "effective_date_missing" in row["issues"]


def test_normalization_preserves_split_terms_and_decimal_source_numbers():
    row = action_row(new_rate=D("4.000"), old_rate=D("1.00"))
    assert row["record_kind"] == "corporate_action"
    assert row["row_key"] == "split-1"
    assert row["fields"]["new_rate"] == D("4.000")
    assert row["fields"]["old_rate"] == D("1.00")
    assert row["date_basis"] == "ex_date"


def test_cross_source_comparison_needs_explicit_identity_and_independence_proof():
    left, right = action_row(), action_row(id="other-event")
    compare = api().compare_corporate_actions
    assert compare(left, right, left_source="alpaca", right_source="vendor2").status == "pending"
    assert compare(left, right, left_source="alpaca", right_source="vendor2",
                   same_event_evidence="verified identity", independence_evidence="separate upstreams").status == "verified"
    assert compare(left, action_row(new_rate="5"), left_source="alpaca", right_source="vendor2",
                   same_event_evidence="verified identity", independence_evidence="separate upstreams").status == "disputed"
    assert compare(left, right, left_source="alpaca", right_source="alpaca",
                   same_event_evidence="verified identity", independence_evidence="claim").status == "pending"


def test_action_bridge_requires_explicit_proof_and_future_events_fail_closed():
    with pytest.raises(ValueError, match="proof"):
        api().corporate_action_from_staged(action_row(), proof=None, cutoff=CUTOFF)
    proof = api().ActionBridgeProof(security_id="s1", identity_evidence="identity-proof",
                                    action_terms_evidence="terms-proof", coverage_evidence="ledger-proof",
                                    source_contract_id="c1", raw_ref="raw1", observation_id="a1", revision=1,
                                    available_at=CAPTURE, captured_at=CAPTURE,
                                    action_type="split", split_ratio=D("4"))
    with pytest.raises(ValueError, match="future"):
        api().corporate_action_from_staged(action_row(ex_date="2026-10-12"), proof=proof, cutoff=CUTOFF)
    event = api().corporate_action_from_staged(action_row(), proof=proof, cutoff=CAPTURE)
    assert event.split_ratio == D("4")
    assert event.available_at == CAPTURE


def test_vwap_bridge_requires_qualification_identity_eligibility_and_completion_evidence():
    from backtest_data.prices.domain import PriceContext
    result = aggregate([page([trade()])])
    with pytest.raises(ValueError, match="proof"):
        api().daily_vwap_stat_from_aggregate(result, proof=None)
    context = PriceContext("s1", "l1", "US", "sip", "regular", "USD", "currency_per_share", "shares", "as_traded")
    proof = api().VWAPBridgeProof(context=context, identity_evidence="identity-proof",
                                  eligibility_evidence="candidate-scope-proof", completeness_evidence="full archive proof",
                                  source_contract_id="c1", raw_ref="all-page-refs", statistic_id="stat1", revision=1,
                                  available_at=CLOSE, captured_at=CAPTURE, materialized_at=CAPTURE,
                                  trade_scope="regular_sale_only_v1")
    stat = api().daily_vwap_stat_from_aggregate(result, proof=proof)
    assert stat.sum_price_volume == result.sum_price_volume
    assert stat.eligible_volume == result.eligible_volume
    assert not stat.historical_replay_supported
    with pytest.raises(ValueError, match="unqualified"):
        api().daily_vwap_stat_from_aggregate(aggregate([page([trade()], next_token="missing")]), proof=proof)


def test_json_decoder_retains_decimal_coefficient_and_raw_page_is_immutable():
    decoded = api().decode_alpaca_json('{"p":0.1000000000000000000000000001,"s":3}')
    assert decoded["p"] == D("0.1000000000000000000000000001")
    assert decoded["s"] == D("3")
    sample = page([trade()])
    with pytest.raises(TypeError):
        sample.payload["trades"][0]["p"] = "changed"


@pytest.mark.parametrize("field", ["x", "i", "z"])
def test_null_source_identity_never_becomes_string_none(field):
    result = aggregate([page([trade(**{field: None})])])
    assert "unresolved_trade_identity" in result.issues
    assert not result.qualified


def test_missing_cash_terms_cannot_be_verified_by_two_matching_sources():
    row = api().normalize_corporate_action_page({"corporate_actions": {"cash_dividends": [{
        "id": "d1", "symbol": "AAPL", "process_date": "2020-08-01", "ex_date": "2020-08-07"}]}},
        cutoff=date(2026, 10, 7))[0]
    assert "cash_terms_missing" in row["issues"]
    comparison = api().compare_corporate_actions(row, row, left_source="alpaca", right_source="vendor2",
        same_event_evidence="identity", independence_evidence="upstream-audit")
    assert comparison.status == "pending"


def test_incomplete_corporate_action_publication_is_not_process_date():
    row = action_row(ex_date=None)
    assert row["effective_date"] is None
    assert row.get("published_date") is None
    assert row["date_basis"] == "unknown"


def test_changed_feed_and_token_cycle_deny_page_chain_qualification():
    original = page([trade()], next_token="p2")
    changed = page([trade(i="2")], token="p2", next_token="p2")
    altered = replace(changed.request, params=tuple((k, "iex" if k == "feed" else v) for k, v in changed.request.params))
    changed = replace(changed, request=altered)
    result = aggregate([original, changed, page([trade(i="3")], token="p2")])
    assert "request_anchor_mismatch" in result.issues
    assert "page_token_cycle" in result.issues
    assert not result.qualified


def test_duplicate_corporate_action_event_in_one_page_is_deduplicated_or_disputed():
    result = api().normalize_corporate_action_page({"corporate_actions": {
        "forward_splits": [action(), action()]}}, cutoff=date(2026, 10, 7))
    assert len(result) == 1
    result = api().normalize_corporate_action_page({"corporate_actions": {
        "forward_splits": [action(), action(new_rate="5")]}}, cutoff=date(2026, 10, 7))
    assert len(result) == 2
    assert all("conflicting_action_identity" in row["issues"] for row in result)


def test_corporate_action_request_pagination_is_checked_separately_from_action_coverage():
    result = api().corporate_action_page_chain_status([])
    assert result.status == "pending"
    spec = api().corporate_action_requests()[0]
    page_type = api().CapturedCorporateActionPage
    first = page_type(spec, {"corporate_actions": {"forward_splits": [action()]}, "next_page_token": "second"}, "raw1", CAPTURE)
    assert api().corporate_action_page_chain_status([first]).status == "pending"
    last = page_type(spec.with_page_token("second"), {"corporate_actions": {}, "next_page_token": None}, "raw2", CAPTURE)
    result = api().corporate_action_page_chain_status([first, last])
    assert result.status == "complete"
    assert result.action_coverage_verified is False


def test_request_freezes_caller_parameter_collection():
    params = [["start", "2016-01-01"]]
    spec = api().AlpacaRequestSpec("GET", "https://data.alpaca.markets/v1/corporate-actions", params)
    before = spec.request_key
    params[0][1] = "2026-01-01"
    assert spec.request_key == before


def test_cross_source_comparison_cannot_ignore_additional_economic_terms():
    left = action_row(additional_cash_rate="2")
    right = action_row(additional_cash_rate="3")
    result = api().compare_corporate_actions(left, right, left_source="alpaca", right_source="vendor2",
        same_event_evidence="identity", independence_evidence="upstream-audit")
    assert result.status == "disputed"


def test_corporate_action_page_chain_rejects_hidden_filter_or_future_interval():
    spec = api().corporate_action_requests()[0]
    for extra in [("cusips", "one-issuer"), ("end", "2026-10-08")]:
        changed = replace(spec, params=tuple((k, v) for k, v in spec.params if k != extra[0]) + (extra,))
        sample = api().CapturedCorporateActionPage(changed,
            {"corporate_actions": {}, "next_page_token": None}, "raw1", CAPTURE)
        result = api().corporate_action_page_chain_status([sample])
        assert result.status == "pending"
        assert "request_anchor_mismatch" in result.issues


def test_cash_currency_unknown_cannot_be_cross_source_verified():
    row = api().normalize_corporate_action_page({"corporate_actions": {"cash_dividends": [{
        "id": "d1", "symbol": "AAPL", "rate": "1", "process_date": "2020-08-01", "ex_date": "2020-08-07"}]}},
        cutoff=date(2026, 10, 7))[0]
    result = api().compare_corporate_actions(row, row, left_source="alpaca", right_source="vendor2",
        same_event_evidence="identity", independence_evidence="upstream-audit")
    assert result.status == "pending"


def test_corporate_action_source_dates_populate_typed_stage_columns():
    row = action_row(record_date="2020-08-24", payable_date="2020-08-28")
    assert row["ex_date"] == date(2020, 8, 31)
    assert row["record_date"] == date(2020, 8, 24)
    assert row["pay_date"] == date(2020, 8, 28)
    assert row["effective_date"] == date(2020, 8, 31)
    assert row["fields"]["process_date"] == "2020-08-28"
    assert row.get("published_date") is None


def test_missing_action_source_dates_remain_typed_null_without_process_fallback():
    row = action_row(ex_date=None, record_date="", payable_date=None)
    assert row["ex_date"] is None
    assert row["record_date"] is None
    assert row["pay_date"] is None
    assert row["effective_date"] is None
    assert row.get("published_date") is None
    assert row["fields"]["process_date"] == "2020-08-28"


@pytest.mark.parametrize("field", ["ex_date", "record_date", "payable_date"])
def test_invalid_action_source_dates_cannot_enter_typed_stage_columns(field):
    with pytest.raises(ValueError):
        action_row(**{field: "2020-02-30"})


@pytest.mark.parametrize("field", ["ex_date", "record_date", "payable_date"])
def test_action_date_columns_do_not_silently_drop_datetime_precision(field):
    with pytest.raises(ValueError, match="precision"):
        action_row(**{field: datetime(2020, 8, 24, tzinfo=timezone.utc)})
