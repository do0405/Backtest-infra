"""Archive and type KRX responses without promoting their identity or units."""
from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ..catalog.models import SourceContract
from ..db import new_id
from ..ingestion.jobs import stable_hash
from ..ingestion.krx_contract import KRX_ENDPOINTS
from ..ingestion.models import IngestionAttempt, RawObject
from ..ingestion.raw_store import RawStore
from .krx_quote_models import KRXQuoteObservation


KRX_SERVICE_PATHS = KRX_ENDPOINTS
KRX_SERVICES = tuple(KRX_SERVICE_PATHS)
KRX_BASE = "https://data-dbg.krx.co.kr"
_QUOTE_NUMERICS = {
    "open": "TDD_OPNPRC", "high": "TDD_HGPRC", "low": "TDD_LWPRC", "close": "TDD_CLSPRC",
    "volume": "ACC_TRDVOL", "notional": "ACC_TRDVAL", "market_cap": "MKTCAP", "listed_shares": "LIST_SHRS",
    "price_change": "CMPPREVDD_PRC", "change_rate": "FLUC_RT",
}
_INDEX_NUMERICS = {
    "open": "OPNPRC_IDX", "high": "HGPRC_IDX", "low": "LWPRC_IDX", "close": "CLSPRC_IDX",
    "volume": "ACC_TRDVOL", "notional": "ACC_TRDVAL", "market_cap": "MKTCAP",
    "price_change": "CMPPREVDD_IDX", "change_rate": "FLUC_RT",
}
_PRODUCT_NUMERICS = {"underlying_index": "OBJ_STKPRC_IDX", "index_change": "CMPPREVDD_IDX", "index_change_rate": "FLUC_RT_IDX"}
_EXTRA_NUMERICS = {"nav": "NAV", "net_asset_total": "INVSTASST_NETASST_TOTAMT",
    "indicative_value": "PER1SECU_INDIC_VAL", "indicative_value_total": "INDIC_VAL_AMT", "par_value": "PARVAL"}
_NUMERIC_COLUMNS = tuple(dict.fromkeys((*_QUOTE_NUMERICS, *_PRODUCT_NUMERICS, *_EXTRA_NUMERICS)))
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_BATCH_SIZE = 250


def krx_record_kind(service):
    if service not in KRX_SERVICE_PATHS:
        raise ValueError("Unknown KRX service")
    return "basic_information" if service.endswith("_basic") else "index_quote" if service.endswith("_index") else "daily_quote"


def _exact_json(value):
    if isinstance(value, Decimal):
        if not value.is_finite():
            return str(value)
        parts = value.as_tuple()
        if len(parts.digits) > 10000 or abs(parts.exponent) > 10000 or value != 0 and abs(value.adjusted()) > 10000:
            # Preserve invalid numeric evidence without unbounded exponent expansion.
            return str(value)
        return format(value, "f")
    if isinstance(value, Mapping):
        return {key: _exact_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_exact_json(item) for item in value]
    if isinstance(value, float):
        # Such a payload cannot supply an exact numeric. Original bytes remain archived.
        return repr(value)
    return value


def _date_field(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}", value):
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        return None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("exact_numeric_required")
    if isinstance(value, str) and not _DECIMAL.fullmatch(value):
        raise ValueError("numeric_missing_or_invalid")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("numeric_missing_or_invalid") from None
    if not result.is_finite():
        raise ValueError("finite_numeric_required")
    parts = result.as_tuple()
    if len(parts.digits) > 10000 or abs(parts.exponent) > 10000 or result != 0 and abs(result.adjusted()) > 10000:
        raise ValueError("numeric_envelope")
    return result


def normalize_krx_quote(service, request_date, row):
    """Copy one row, preserving invalid cells and avoiding arithmetic coercion."""
    kind = krx_record_kind(service)
    parsed = {column: None for column in _NUMERIC_COLUMNS}
    parsed.update(record_kind=kind, reported_date=None, source_code=None, source_name=None, source_isin=None,
                  source_english_name=None, index_class=None, listing_date=None, issues=[], status="source_typed")
    if not isinstance(row, Mapping):
        parsed.update(raw_fields={"_source_value": _exact_json(row)}, status="source_issues",
                      issues=[{"field": None, "rule": "row_object_required"}])
        return parsed
    parsed["raw_fields"] = _exact_json(row)
    issues = parsed["issues"]
    def issue(field, rule):
        issues.append({"field": field, "rule": rule})
    def source_text(field, *, required=False):
        value = row.get(field)
        if isinstance(value, str):
            if required and not value:
                issue(field, "source_identifier_or_name_required")
            return value
        if value is not None or required:
            issue(field, "source_text_required")
        return None
    if kind == "index_quote":
        parsed["index_class"] = source_text("IDX_CLSS", required=True)
        parsed["source_name"] = source_text("IDX_NM", required=True)
        if parsed["index_class"] is not None and parsed["source_name"] is not None:
            parsed["source_code"] = parsed["index_class"] + ":" + parsed["source_name"]
        numerics = _INDEX_NUMERICS
    else:
        code_field = "ISU_SRT_CD" if kind == "basic_information" else "ISU_CD"
        parsed["source_code"] = source_text(code_field, required=True)
        parsed["source_name"] = source_text("ISU_NM", required=True)
        if parsed["source_code"] is not None and not re.fullmatch(r"[0-9]{6}", parsed["source_code"]):
            issue(code_field, "six_digit_source_code_required")
        numerics = _QUOTE_NUMERICS
    if kind == "basic_information":
        parsed["source_isin"] = source_text("ISU_CD", required=True)
        parsed["source_english_name"] = source_text("ISU_ENG_NM")
        if parsed["source_isin"] is not None and not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", parsed["source_isin"]):
            issue("ISU_CD", "source_isin_format")
        parsed["listing_date"] = _date_field(row.get("LIST_DD"))
        if parsed["listing_date"] is None:
            issue("LIST_DD", "listing_date_missing_or_invalid")
        elif parsed["listing_date"] > request_date:
            issue("LIST_DD", "listing_date_after_requested_snapshot")
        numerics = {"par_value": "PARVAL", "listed_shares": "LIST_SHRS"}
    if kind != "basic_information" or "BAS_DD" in row:
        parsed["reported_date"] = _date_field(row.get("BAS_DD"))
        if parsed["reported_date"] is None:
            issue("BAS_DD", "reported_date_missing_or_invalid")
        elif parsed["reported_date"] != request_date:
            issue("BAS_DD", "reported_date_request_mismatch")
    numerics = dict(numerics)
    if service in {"etf", "etn"}:
        numerics.update(_PRODUCT_NUMERICS)
        numerics.update({"nav": "NAV", "net_asset_total": "INVSTASST_NETASST_TOTAMT"} if service == "etf" else
                        {"indicative_value": "PER1SECU_INDIC_VAL", "indicative_value_total": "INDIC_VAL_AMT"})
    for column, field in numerics.items():
        try:
            parsed[column] = _number(row.get(field))
        except ValueError as exc:
            issue(field, str(exc))
    if issues:
        parsed["status"] = "source_issues"
    return parsed


def krx_quote_specification(service):
    kind = krx_record_kind(service)
    numerics = {"par_value": "PARVAL", "listed_shares": "LIST_SHRS"} if kind == "basic_information" else (
               _INDEX_NUMERICS if kind == "index_quote" else _QUOTE_NUMERICS)
    if service in {"etf", "etn"}:
        numerics = dict(numerics) | _PRODUCT_NUMERICS | ({"nav": "NAV", "net_asset_total": "INVSTASST_NETASST_TOTAMT"} if service == "etf" else
                    {"indicative_value": "PER1SECU_INDIC_VAL", "indicative_value_total": "INDIC_VAL_AMT"})
    return {"endpoint": KRX_BASE + KRX_SERVICE_PATHS[service], "method": "GET", "service": service,
        "record_kind": kind, "request_date_parameter": "basDd", "response_envelope": "OutBlock_1",
        "date_field": None if kind == "basic_information" else "BAS_DD",
        "identifier_fields": ["ISU_CD", "ISU_SRT_CD"] if kind == "basic_information" else (
            ["IDX_CLSS", "IDX_NM"] if kind == "index_quote" else ["ISU_CD"]),
        "numeric_fields": numerics, "source_grade": "empirical_response_pending_units_and_history",
        "empirical_response_date": "2026-10-07", "empirical_check_date": "2026-10-10",
        "units_verified": False, "historical_identity_verified": False,
        "official_calendar_verified": False, "coverage_complete": False, "canonical_price_eligible": False,
        "request_date_role": "unverified_source_snapshot_date" if kind == "basic_information" else "requested_source_quote_date",
        "price_unit_status": "not_applicable" if kind == "basic_information" else (
            "unverified_index_points" if kind == "index_quote" else "unknown"),
        "quantity_unit_status": "unknown", "notional_unit_status": "unknown", "trade_scope_status": "unknown",
        "availability": "capture_only", "historical_lifecycle_qualified": False,
        "references": ["https://openapi.krx.co.kr/", KRX_BASE + KRX_SERVICE_PATHS[service]]}


def _contract(session, service):
    specification = krx_quote_specification(service)
    digest = stable_hash(specification)
    version = "v1:" + digest[:16]
    session.execute(insert(SourceContract).values(id=new_id(), source="krx", domain="market_quotes", market="KR",
        version=version, specification=specification, content_hash=digest, qualification="empirical_pending")
        .on_conflict_do_nothing(index_elements=[SourceContract.source, SourceContract.version, SourceContract.domain, SourceContract.market]))
    return session.scalar(select(SourceContract).where(SourceContract.source == "krx", SourceContract.domain == "market_quotes",
        SourceContract.market == "KR", SourceContract.version == version))


def store_krx_quotes(session, service, request_date, response, payload, raw_root, *, fence=None):
    """Stage every row in bounded inserts within the caller's fenced transaction.

    Same response capture replays are idempotent; a later capture remains distinct
    even when its bytes are identical. A successful stage certifies receipt only.
    """
    kind = krx_record_kind(service)
    if type(request_date) is not date:
        raise ValueError("KRX request date required")
    if not isinstance(response.captured_at, datetime) or response.captured_at.tzinfo is None or response.captured_at.utcoffset() is None:
        raise ValueError("KRX capture requires a timezone-aware timestamp")
    captured_at = response.captured_at.astimezone(timezone.utc)
    if fence:
        fence()
    contract = _contract(session, service)
    stored = RawStore(raw_root).put(response.content)
    session.execute(insert(RawObject).values(id=new_id(), content_hash=stored.content_hash, storage_key=stored.storage_key,
        byte_count=stored.byte_count).on_conflict_do_nothing(index_elements=[RawObject.content_hash]))
    raw_id = session.scalar(select(RawObject.id).where(RawObject.content_hash == stored.content_hash))
    response_valid = isinstance(payload, Mapping) and isinstance(payload.get("OutBlock_1"), list) and response.status_code == 200
    rows = payload["OutBlock_1"] if isinstance(payload, Mapping) and isinstance(payload.get("OutBlock_1"), list) else []
    endpoint, params = KRX_BASE + KRX_SERVICE_PATHS[service], {"basDd": request_date.strftime("%Y%m%d")}
    capture_hash = stable_hash(["krx_source_capture_v1", contract.content_hash, endpoint, params, stored.content_hash,
                                captured_at.isoformat(), response.status_code])
    capture_id = str(uuid5(NAMESPACE_URL, "backtest:krx:" + capture_hash))
    session.execute(insert(IngestionAttempt).values(id=capture_id, source_contract_id=contract.id,
        request_hash=stable_hash([endpoint, params]), content_hash=stored.content_hash,
        status="received" if response_valid else "invalid_response", captured_at=captured_at,
        evidence={"endpoint": endpoint, "params": params, "service": service, "record_kind": kind,
            "response_valid": response_valid, "source_row_count": len(rows), "capture_hash": capture_hash,
            "raw_id": raw_id, "request_date": request_date.isoformat(), "coverage_complete": False,
            "official_calendar_verified": False, "historical_identity_verified": False})
        .on_conflict_do_nothing(index_elements=[IngestionAttempt.id]))
    pending, inserted_ids, issue_rows = [], [], 0
    def flush():
        if pending:
            inserted_ids.extend(session.scalars(insert(KRXQuoteObservation).values(pending)
                .on_conflict_do_nothing(index_elements=[KRXQuoteObservation.signature]).returning(KRXQuoteObservation.id)))
            pending.clear()
            if fence:
                fence()
    specification = contract.specification
    for index, row in enumerate(rows):
        parsed = normalize_krx_quote(service, request_date, row)
        if not response_valid:
            parsed["issues"].append({"field": None, "rule": "response_status_invalid"})
            parsed["status"] = "source_issues"
        issue_rows += bool(parsed["issues"])
        evidence = {"security_identity_status": "unresolved_historical_identity", "historical_identity_verified": False,
            "units_verified": False, "coverage_complete": False, "official_calendar_verified": False,
            "canonical_price_eligible": False, "request_date_role": specification["request_date_role"],
            "price_unit_status": specification["price_unit_status"], "quantity_unit_status": "unknown",
            "notional_unit_status": "unknown", "trade_scope_status": "unknown", "source_grade": specification["source_grade"],
            "available_at_semantics": "capture_only", "raw_numeric_values": "exact_strings_or_decimal_fixed_point",
            "source_row_hash": stable_hash(parsed["raw_fields"])}
        if kind == "index_quote" and isinstance(row, Mapping):
            evidence["identifier_components"] = _exact_json({key: row.get(key) for key in ("IDX_CLSS", "IDX_NM")})
        pending.append({"id": new_id(), "source_contract_id": contract.id, "raw_id": raw_id, "response_capture_id": capture_id,
            "service": service, "request_date": request_date, "row_index": index, **parsed,
            "signature": stable_hash(["krx_source_row_v1", capture_hash, index, parsed["raw_fields"]]),
            "captured_at": captured_at, "coverage_complete": False, "evidence": evidence})
        if len(pending) == _BATCH_SIZE:
            flush()
    flush()
    if fence:
        fence()
    return {"inserted": len(inserted_ids), "inserted_ids": inserted_ids, "source_row_count": len(rows), "issue_rows": issue_rows,
        "response_valid": response_valid, "response_capture_id": capture_id, "raw_id": raw_id,
        "request_date": request_date.isoformat(), "service": service, "record_kind": kind,
        "empty_response": response_valid and not rows, "request_staged": response_valid,
        "coverage_complete": False, "official_calendar_verified": False, "historical_identity_verified": False,
        "units_verified": False, "canonical_price_eligible": False}
