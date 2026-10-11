from datetime import date
from decimal import Decimal, localcontext
import importlib
import importlib.util

import pytest


def storage():
    assert importlib.util.find_spec("backtest_data.prices.krx_quote_storage") is not None, "KRX quote staging is required"
    return importlib.import_module("backtest_data.prices.krx_quote_storage")


def quote(**changes):
    return {"BAS_DD": "20261007", "ISU_CD": "095570", "ISU_NM": "AJ네트웍스",
        "TDD_OPNPRC": "4285.0100", "TDD_HGPRC": "4400", "TDD_LWPRC": "4200",
        "TDD_CLSPRC": "4285", "ACC_TRDVOL": "41462", "ACC_TRDVAL": "177183550",
        "MKTCAP": "193908072315", "LIST_SHRS": "45252759", "CMPPREVDD_PRC": "-15",
        "FLUC_RT": "-0.35"} | changes


def test_source_numeric_values_are_exact_and_context_independent():
    module = storage()
    with localcontext() as context:
        context.prec = 4
        parsed = module.normalize_krx_quote("kospi", date(2026, 10, 7), quote(
            ACC_TRDVAL="123456789012345678901234567890.0100", EXTRA=Decimal("1E-7")))
    assert parsed["open"] == Decimal("4285.0100")
    assert parsed["notional"] == Decimal("123456789012345678901234567890.0100")
    assert parsed["price_change"] == Decimal("-15")
    assert parsed["raw_fields"]["ACC_TRDVAL"] == "123456789012345678901234567890.0100"
    assert parsed["raw_fields"]["EXTRA"] == "0.0000001"
    assert parsed["source_code"] == "095570"
    assert parsed["reported_date"] == date(2026, 10, 7)
    assert parsed["issues"] == []


@pytest.mark.parametrize("value", [None, "", "-", "n/a", "NaN", "Infinity", 1.1, True, "1e20000"])
def test_bad_numeric_value_is_retained_with_an_issue_and_never_zero_filled(value):
    parsed = storage().normalize_krx_quote("kospi", date(2026, 10, 7), quote(TDD_CLSPRC=value))
    assert parsed["close"] is None
    assert any(issue["field"] == "TDD_CLSPRC" for issue in parsed["issues"])
    assert "TDD_CLSPRC" in parsed["raw_fields"]
    assert parsed["status"] == "source_issues"


@pytest.mark.parametrize("service, extras, expected", [
    ("etf", {"NAV": "100.00100", "INVSTASST_NETASST_TOTAMT": "99999999999999999999",
             "OBJ_STKPRC_IDX": "341.20", "CMPPREVDD_IDX": "-3.1", "FLUC_RT_IDX": "-0.90"}, "nav"),
    ("etn", {"PER1SECU_INDIC_VAL": "101.00200", "INDIC_VAL_AMT": "19999999999999999999",
             "OBJ_STKPRC_IDX": "341.20", "CMPPREVDD_IDX": "-3.1", "FLUC_RT_IDX": "-0.90"}, "indicative_value"),
])
def test_product_values_are_typed_without_claiming_price_or_quantity_units(service, extras, expected):
    parsed = storage().normalize_krx_quote(service, date(2026, 10, 7), quote(**extras))
    assert parsed[expected] == Decimal(next(value for key, value in extras.items() if key in {"NAV", "PER1SECU_INDIC_VAL"}))
    assert parsed["underlying_index"] == Decimal("341.20")
    assert parsed["issues"] == []


@pytest.mark.parametrize("changes", [{"BAS_DD": "20261006"}, {"BAS_DD": "20260230"}, {"ISU_CD": "KR7095570009"}, {"ISU_CD": None}])
def test_date_and_source_code_gaps_preserve_the_row(changes):
    parsed = storage().normalize_krx_quote("kospi", date(2026, 10, 7), quote(**changes))
    assert parsed["status"] == "source_issues"
    assert parsed["raw_fields"] == quote(**changes)


def test_nil_row_is_preserved_as_source_evidence():
    parsed = storage().normalize_krx_quote("kospi", date(2026, 10, 7), None)
    assert parsed["raw_fields"] == {"_source_value": None}
    assert parsed["close"] is None and parsed["source_code"] is None
    assert parsed["issues"] == [{"field": None, "rule": "row_object_required"}]


def test_basic_listing_date_after_requested_snapshot_is_a_gap():
    parsed = storage().normalize_krx_quote("kospi_basic", date(2016, 1, 1), {
        "ISU_CD": "KR7095570009", "ISU_SRT_CD": "095570", "ISU_NM": "AJ네트웍스",
        "LIST_DD": "20201007", "PARVAL": "1000", "LIST_SHRS": "45252759"})
    assert parsed["reported_date"] is None
    assert parsed["listing_date"] == date(2020, 10, 7)
    assert {"field": "LIST_DD", "rule": "listing_date_after_requested_snapshot"} in parsed["issues"]
    assert parsed["close"] is None and parsed["record_kind"] == "basic_information"


def test_out_of_envelope_decimal_is_retained_without_expanding_its_exponent():
    parsed = storage().normalize_krx_quote("kospi", date(2026, 10, 7), quote(TDD_CLSPRC=Decimal("1e20000")))
    assert parsed["close"] is None
    assert parsed["raw_fields"]["TDD_CLSPRC"] == "1E+20000"
    assert {"field": "TDD_CLSPRC", "rule": "numeric_envelope"} in parsed["issues"]
