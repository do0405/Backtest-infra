from datetime import date

import pytest

from backtest_data.fundamentals.adapters.opendart import parse_financial_json


def payload(**fields):
    return {"status": "000", "list": [{"rcept_no": "20260814000123", "corp_code": "00123456",
        "sj_div": "IS", "account_id": "ifrs-full_Revenue", "currency": "KRW", **fields}]}


def test_prior_comparable_quarter_amount_is_not_lost():
    result = parse_financial_json(payload(frmtrm_q_amount="125", frmtrm_add_amount="250"),
        requested_receipt="20260814000123")
    assert {fact.evidence["amount_field"] for fact in result.facts} == {"frmtrm_q_amount", "frmtrm_add_amount"}
    assert result.inventory.numeric_nodes == result.inventory.extracted_nodes == 2


def test_scope_is_taken_from_verified_request_when_response_omits_fs_div():
    result = parse_financial_json(payload(thstrm_amount="100"), requested_receipt="20260814000123",
                                  requested_fs_div="CFS", requested_corp_code="00123456")
    assert result.facts[0].scope == "consolidated"
    assert result.contexts[result.facts[0].context_id].entity_identifier == "00123456"
    assert result.facts[0].evidence["requested_fs_div"] == "CFS"


def test_corporation_mismatch_cannot_be_assigned_to_requested_issuer():
    with pytest.raises(ValueError, match="corp_code_mismatch"):
        parse_financial_json(payload(thstrm_amount="100"), requested_receipt="20260814000123",
                             requested_corp_code="00999999")


def test_response_cannot_override_verified_request_scope():
    with pytest.raises(ValueError, match="financial_scope_mismatch"):
        parse_financial_json(payload(thstrm_amount="100", fs_div="OFS"), requested_receipt="20260814000123",
                             requested_fs_div="CFS")


def test_same_amount_field_uses_statement_specific_period():
    data = payload(thstrm_amount="100")
    data["list"].append({**data["list"][0], "sj_div": "BS", "account_id": "ifrs-full_Assets"})
    result = parse_financial_json(data, requested_receipt="20260814000123", periods={
        ("IS", "thstrm_amount"): ("quarter", date(2026, 4, 1), date(2026, 6, 30)),
        ("BS", "thstrm_amount"): ("instant", None, date(2026, 6, 30)),
    })
    assert [fact.period_kind for fact in result.facts] == ["quarter", "instant"]
    assert result.facts[1].period_start is None


def test_unknown_fiscal_dates_are_preserved_without_calendar_guess():
    result = parse_financial_json(payload(thstrm_amount="100", thstrm_nm="제 3기 반기"),
                                  requested_receipt="20260814000123", requested_fs_div="CFS")
    assert result.facts[0].period_kind == "unknown" and result.facts[0].period_end is None
