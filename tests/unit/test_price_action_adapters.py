from datetime import date
from decimal import Decimal as D

import pytest

from backtest_data.prices.action_adapters import ActionFieldContract, normalize_action_revision
from test_corporate_actions import at


def contract():
    return ActionFieldContract("verified vendor contract v1",
                               (("event_id", "event"), ("action_type", "type"), ("effective_date", "effective"),
                                ("ex_date", "ex"), ("split_ratio", "ratio"), ("cash_amount", "cash"), ("currency", "currency")),
                               (("S", "split"), ("D", "cash_dividend")), "%Y-%m-%d")


def normalize(row):
    return normalize_action_revision(row, contract=contract(), observation_id="a1", security_id="s1", revision=1,
                                      available_at=at(2), captured_at=at(5), source_contract_id="c1", raw_ref="hash1")


def test_verified_mapping_parses_ratio_without_defaulting_missing_values():
    event = normalize({"event": "event1", "type": "S", "effective": "2024-01-04", "ex": "2024-01-04", "ratio": "2"})
    assert event.split_ratio == D("2")
    assert event.cash_amount is None
    assert event.effective_date == date(2024, 1, 4)
    with pytest.raises(ValueError):
        normalize({"event": "event1", "type": "S", "effective": "2024-01-04"})


def test_unknown_action_code_is_preserved_and_not_mapped_to_split():
    event = normalize({"event": "event1", "type": "COMPLEX_NEW", "effective": "2024-01-04"})
    assert event.action_type == "unsupported:COMPLEX_NEW"
    assert event.split_ratio is None
    assert event.cash_amount is None


def test_field_contract_requires_verification_and_no_duplicate_canonical_keys():
    with pytest.raises(ValueError):
        ActionFieldContract("", (), (), "%Y-%m-%d")
    with pytest.raises(ValueError):
        ActionFieldContract("evidence", (("event_id", "a"), ("event_id", "b")), (), "%Y-%m-%d")
