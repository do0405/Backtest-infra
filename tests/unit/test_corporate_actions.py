from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest

from backtest_data.prices.actions import CorporateAction, select_visible_actions


def at(day):
    return datetime(2024, 1, day, tzinfo=timezone.utc)


def action(**changes):
    return replace(CorporateAction("a1", "event1", "s1", "split", 1, at(2), at(10), date(2024, 1, 4), date(2024, 1, 4), "c1", "hash1", split_ratio=D("2")), **changes)


def coverage(**changes):
    from backtest_data.prices.actions import ActionCoverage

    return replace(ActionCoverage("s1", date(2024, 1, 1), date(2024, 1, 31), "c1", "verified complete corporate-action/no-event ledger",
                                  at(2), at(3), verified=True, decision_recorded_at=at(4)), **changes)


def test_revisions_are_visible_by_public_cutoff_not_latest_archive():
    revised = action(id="a2", revision=2, available_at=at(6), split_ratio=D("3"))
    assert select_visible_actions([action(), revised], as_of=at(5))[0].split_ratio == 2
    assert select_visible_actions([action(), revised], as_of=at(7))[0].split_ratio == 3


def test_observed_mode_requires_capture_and_recorded_decision():
    assert select_visible_actions([action()], as_of=at(5), history_mode="observed_system") == ()
    captured = action(captured_at=at(3))
    assert select_visible_actions([captured], as_of=at(5), history_mode="observed_system") == ()
    assert select_visible_actions([captured], as_of=at(5), history_mode="observed_system", decision_times={"a1": at(4)}) == (captured,)
    assert select_visible_actions([captured], as_of=at(5), history_mode="observed_system", decision_times={"a1": at(6)}) == ()


def test_effective_dates_and_unsupported_unknown_actions_are_preserved():
    unknown = action(id="u1", event_id="event2", action_type="vendor_new_event", split_ratio=None)
    future = action(id="f1", event_id="event3", effective_date=date(2024, 1, 20))
    assert select_visible_actions([unknown, future], as_of=at(5)) == (unknown,)
    with pytest.raises(ValueError):
        action(split_ratio=D("0"))


def test_conflicting_same_event_revision_is_not_arbitrarily_selected():
    with pytest.raises(ValueError, match="conflicting"):
        select_visible_actions([action(), action(id="a2", split_ratio=D("3"))], as_of=at(5))


def test_revision_that_moves_effective_date_or_cancels_does_not_resurrect_original():
    delayed = action(id="a2", revision=2, available_at=at(5), effective_date=date(2024, 1, 20))
    assert select_visible_actions([action(), delayed], as_of=at(6)) == ()
    cancelled = action(id="a2", revision=2, available_at=at(5), cancelled=True)
    assert select_visible_actions([action(), cancelled], as_of=at(6)) == ()


def test_action_coverage_is_explicit_and_validates_its_interval():
    with pytest.raises(ValueError, match="coverage"):
        coverage(from_session=date(2024, 2, 1))
    with pytest.raises(ValueError, match="evidence"):
        coverage(evidence_ref="")
