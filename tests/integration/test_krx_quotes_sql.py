from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import importlib
import importlib.util
import json

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError

from backtest_data.catalog.models import SourceContract
from backtest_data.ingestion.http import ProviderResponse
from backtest_data.ingestion.jobs import LeaseLost
from backtest_data.ingestion.models import IngestionAttempt, RawObject
from backtest_data.ingestion.raw_store import RawStore
from backtest_data.prices.models import PriceObservation, VWAPDailyStat
from backtest_data.releases.models import SelectionDecision


pytestmark = pytest.mark.integration
DAY = date(2026, 10, 7)
CAPTURE = datetime(2026, 10, 10, 3, tzinfo=timezone.utc)


def modules():
    assert importlib.util.find_spec("backtest_data.prices.krx_quote_storage") is not None, "KRX SQL stage is required"
    return (importlib.import_module("backtest_data.prices.krx_quote_storage"),
            importlib.import_module("backtest_data.prices.krx_quote_models"))


def quote(**changes):
    return {"BAS_DD": "20261007", "ISU_CD": "095570", "ISU_NM": "AJ네트웍스",
        "TDD_OPNPRC": "4285.0100", "TDD_HGPRC": "4400", "TDD_LWPRC": "4200",
        "TDD_CLSPRC": "4285", "ACC_TRDVOL": "41462", "ACC_TRDVAL": "177183550",
        "MKTCAP": "193908072315", "LIST_SHRS": "45252759", "CMPPREVDD_PRC": "-15", "FLUC_RT": "-0.35"} | changes


def response(payload, capture=CAPTURE):
    return ProviderResponse(json.dumps(payload, ensure_ascii=False).encode("utf-8"), 200, capture, "application/json")


def test_all_five_services_store_typed_source_rows_without_canonical_promotion(db_session, tmp_path):
    module, models = modules()
    before = {table: db_session.scalar(select(func.count()).select_from(table))
              for table in (PriceObservation, VWAPDailyStat, SelectionDecision)}
    for service in ("kospi", "kosdaq", "konex", "etf", "etn"):
        extra = {"NAV": "100.00100", "INVSTASST_NETASST_TOTAMT": "88888888888888888888888"} if service == "etf" else (
                {"PER1SECU_INDIC_VAL": "102.0100", "INDIC_VAL_AMT": "77777777777777777777777"} if service == "etn" else {})
        if service in {"etf", "etn"}:
            extra |= {"OBJ_STKPRC_IDX": "123.40", "CMPPREVDD_IDX": "-0.01", "FLUC_RT_IDX": "-0.008", "IDX_IND_NM": "지수"}
        payload = {"OutBlock_1": [quote(**extra)]}
        received = response(payload)
        result = module.store_krx_quotes(db_session, service, DAY, received, payload, tmp_path)
        assert result["inserted"] == 1 and result["source_row_count"] == 1
        assert result["coverage_complete"] is False and result["historical_identity_verified"] is False
        row = db_session.scalar(select(models.KRXQuoteObservation).where(models.KRXQuoteObservation.service == service))
        assert row.request_date == row.reported_date == DAY
        assert row.source_code == "095570" and row.source_name == "AJ네트웍스"
        assert row.open == Decimal("4285.0100") and row.notional == Decimal("177183550")
        assert row.market_cap == Decimal("193908072315") and row.listed_shares == Decimal("45252759")
        assert row.status == "source_typed" and row.issues == []
        assert row.evidence["units_verified"] is False and row.evidence["coverage_complete"] is False
        assert row.evidence["security_identity_status"] == "unresolved_historical_identity"
        assert "security_id" not in row.__table__.columns
        raw = db_session.get(RawObject, row.raw_id)
        assert RawStore(tmp_path).read(raw.storage_key) == received.content
        capture = db_session.get(IngestionAttempt, row.response_capture_id)
        assert capture.source_contract_id == row.source_contract_id
        assert "AUTH_KEY" not in json.dumps(capture.evidence)
        contract = db_session.get(SourceContract, row.source_contract_id)
        assert contract.domain == "market_quotes" and contract.qualification == "empirical_pending"
        assert contract.specification["units_verified"] is False
        if service == "etf":
            assert row.nav == Decimal("100.00100") and row.net_asset_total == Decimal("88888888888888888888888")
        if service == "etn":
            assert row.indicative_value == Decimal("102.0100") and row.indicative_value_total == Decimal("77777777777777777777777")
    assert {table: db_session.scalar(select(func.count()).select_from(table)) for table in before} == before


@pytest.mark.parametrize("service", ["kospi_basic", "kosdaq_basic"])
def test_basic_information_has_typed_values_and_no_inferred_historical_identity(db_session, tmp_path, service):
    module, models = modules()
    payload = {"OutBlock_1": [{"ISU_CD": "KR7095570009", "ISU_SRT_CD": "095570", "ISU_NM": "AJ네트웍스",
        "ISU_ENG_NM": "AJ Networks", "LIST_DD": "20150821", "PARVAL": "1000", "LIST_SHRS": "45252759"}]}
    result = module.store_krx_quotes(db_session, service, DAY, response(payload), payload, tmp_path)
    row = db_session.get(models.KRXQuoteObservation, result["inserted_ids"][0])
    assert row.record_kind == "basic_information" and row.status == "source_typed"
    assert row.source_code == "095570" and row.source_isin == "KR7095570009"
    assert row.reported_date is None and row.request_date == DAY
    assert row.listing_date == date(2015, 8, 21) and row.par_value == Decimal("1000")
    assert row.open is None and row.close is None and row.volume is None
    assert row.evidence["request_date_role"] == "unverified_source_snapshot_date"
    assert row.evidence["historical_identity_verified"] is False


@pytest.mark.parametrize("service", ["krx_index", "kospi_index", "kosdaq_index"])
def test_indices_preserve_exact_identifier_and_null_levels(db_session, tmp_path, service):
    module, models = modules()
    payload = {"OutBlock_1": [{"BAS_DD": "20261007", "IDX_CLSS": "KOSPI", "IDX_NM": "코스피 (외국주포함)",
        "CLSPRC_IDX": "", "OPNPRC_IDX": "123.45", "HGPRC_IDX": "124.00100", "LWPRC_IDX": "122",
        "CMPPREVDD_IDX": "-0.45", "FLUC_RT": "-0.01", "ACC_TRDVOL": "269959050", "ACC_TRDVAL": "20576543466151",
        "MKTCAP": "5612934025523264"}]}
    result = module.store_krx_quotes(db_session, service, DAY, response(payload), payload, tmp_path)
    row = db_session.get(models.KRXQuoteObservation, result["inserted_ids"][0])
    assert row.record_kind == "index_quote" and row.status == "source_issues"
    assert row.index_class == "KOSPI" and row.source_name == "코스피 (외국주포함)"
    assert row.close is None and row.high == Decimal("124.00100")
    assert row.price_change == Decimal("-0.45")
    assert row.evidence["price_unit_status"] == "unverified_index_points"
    assert row.evidence["identifier_components"] == {"IDX_CLSS": "KOSPI", "IDX_NM": "코스피 (외국주포함)"}


def test_same_capture_replay_is_idempotent_and_recapture_preserves_new_evidence(db_session, tmp_path):
    module, models = modules()
    payload = {"OutBlock_1": [quote(), quote()]}
    received = response(payload)
    first = module.store_krx_quotes(db_session, "kospi", DAY, received, payload, tmp_path)
    again = module.store_krx_quotes(db_session, "kospi", DAY, received, payload, tmp_path)
    newer = module.store_krx_quotes(db_session, "kospi", DAY, response(payload, CAPTURE + timedelta(days=1)), payload, tmp_path)
    assert first["inserted"] == newer["inserted"] == 2 and again["inserted"] == 0
    rows = db_session.scalars(select(models.KRXQuoteObservation)).all()
    assert len(rows) == 4 and len({row.signature for row in rows}) == 4
    assert sorted(row.row_index for row in rows) == [0, 0, 1, 1]
    assert len({row.response_capture_id for row in rows}) == 2


def test_bad_and_nil_rows_are_stored_and_reported_as_source_issues(db_session, tmp_path):
    module, models = modules()
    payload = {"OutBlock_1": [quote(TDD_CLSPRC="-"), None, quote(ISU_CD="KR7095570009", BAS_DD="20261006")]}
    result = module.store_krx_quotes(db_session, "kosdaq", DAY, response(payload), payload, tmp_path)
    assert result["inserted"] == result["source_row_count"] == result["issue_rows"] == 3
    rows = db_session.scalars(select(models.KRXQuoteObservation).order_by(models.KRXQuoteObservation.row_index)).all()
    assert rows[0].close is None and rows[0].raw_fields["TDD_CLSPRC"] == "-"
    assert rows[1].raw_fields == {"_source_value": None} and rows[1].close is None
    assert rows[2].source_code == "KR7095570009" and rows[2].reported_date == date(2026, 10, 6)
    assert all(row.status == "source_issues" and row.issues for row in rows)


@pytest.mark.parametrize("payload, valid", [({"OutBlock_1": []}, True), ({}, False), ({"OutBlock_1": None}, False)])
def test_empty_or_invalid_response_has_raw_capture_but_no_holiday_or_coverage_claim(db_session, tmp_path, payload, valid):
    module, models = modules()
    result = module.store_krx_quotes(db_session, "konex", DAY, response(payload), payload, tmp_path)
    assert result["response_valid"] is valid and result["inserted"] == 0
    assert result["coverage_complete"] is False and result["official_calendar_verified"] is False
    assert db_session.get(IngestionAttempt, result["response_capture_id"]) is not None
    assert db_session.get(RawObject, result["raw_id"]) is not None


@pytest.mark.parametrize("operation", ["UPDATE krx_quote_observation SET status='changed'", "DELETE FROM krx_quote_observation"])
def test_source_quote_rows_reject_update_and_delete(db_session, tmp_path, operation):
    module, models = modules()
    payload = {"OutBlock_1": [quote()]}
    module.store_krx_quotes(db_session, "kospi", DAY, response(payload), payload, tmp_path)
    with pytest.raises(DBAPIError, match="immutable evidence"), db_session.begin_nested():
        db_session.execute(text(operation))


def test_source_quote_inserts_are_bounded_and_lease_loss_rolls_back(db_session, tmp_path):
    module, models = modules()
    payload = {"OutBlock_1": [quote(ISU_CD=f"{index:06d}") for index in range(751)]}
    queries = []
    def count(connection, cursor, statement, parameters, context, many):
        if context.isinsert and context.compiled.statement.table.name == "krx_quote_observation":
            queries.append(len(parameters))
    event.listen(db_session.bind, "before_cursor_execute", count)
    calls = []
    try:
        result = module.store_krx_quotes(db_session, "kospi", DAY, response(payload), payload, tmp_path, fence=lambda: calls.append(True))
    finally:
        event.remove(db_session.bind, "before_cursor_execute", count)
    assert result["inserted"] == 751
    assert 2 <= len(queries) <= 4 and all(value <= 500 * len(models.KRXQuoteObservation.__table__.columns) for value in queries)
    assert len(calls) >= 3
    before = db_session.scalar(select(func.count()).select_from(models.KRXQuoteObservation))
    def lost():
        raise LeaseLost("Expired or superseded lease")
    with pytest.raises(LeaseLost), db_session.begin_nested():
        module.store_krx_quotes(db_session, "kospi", DAY, response(payload, CAPTURE + timedelta(days=1)), payload, tmp_path, fence=lost)
    assert db_session.scalar(select(func.count()).select_from(models.KRXQuoteObservation)) == before
