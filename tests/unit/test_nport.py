from datetime import date
from decimal import Decimal
import importlib
import zipfile

import pytest


ACC = "0001234567-19-000001"


def nport():
    name = "backtest_data.supplemental.nport"
    assert importlib.util.find_spec(name) is not None, "bounded N-PORT streaming is required"
    return importlib.import_module(name)


def archive(tmp_path, *, report="2019-09-30", holding_count=2, extra=None, balance="123456789012345678901234.123456789012"):
    files = {
        "SUBMISSION.tsv": "ACCESSION_NUMBER\tFILING_DATE\tREPORT_DATE\tSUB_TYPE\n" + ACC + "\t2019-11-29\t" + report + "\tNPORT-P\n",
        "REGISTRANT.tsv": "ACCESSION_NUMBER\tCIK\tREGISTRANT_NAME\n" + ACC + "\t0001234567\tHistorical Trust\n",
        "FUND_REPORTED_INFO.tsv": "ACCESSION_NUMBER\tSERIES_ID\tSERIES_NAME\tNET_ASSETS\tTOTAL_ASSETS\tTOTAL_LIABILITIES\n" + ACC + "\tS000012345\tFund Without Current Ticker\t100.000000000001\t120.01\t20.009999999999\n",
        "FUND_REPORTED_HOLDING.tsv": "ACCESSION_NUMBER\tHOLDING_ID\tISSUER_NAME\tISSUER_CUSIP\tBALANCE\tUNIT\tCURRENCY_CODE\tCURRENCY_VALUE\tPERCENTAGE\n" + "".join(ACC + f"\t{i}\t서울 Issuer\t012345678\t{balance}\tNS\tUSD\t99.000000000001\t12.345678901234\n" for i in range(holding_count)),
        "IDENTIFIERS.tsv": "HOLDING_ID\tIDENTIFIERS_ID\tIDENTIFIER_ISIN\tIDENTIFIER_TICKER\n0\t9\tUS0123456789\tOLD\n",
    }
    if extra:
        files.update(extra)
    path = tmp_path / "2019q4_nport.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, value in files.items():
            z.writestr(name, value)
    return path


def test_filing_day_is_not_publication_and_historical_series_identity_is_joined(tmp_path):
    rows = list(nport().iter_nport_rows(archive(tmp_path)))
    holding = next(r for r in rows if r["source_section"] == "FUND_REPORTED_HOLDING")
    assert holding["entity_key"] == "S000012345"
    assert holding["entity_name"] == "Fund Without Current Ticker"
    assert holding["report_date"] == date(2019, 9, 30)
    assert holding["filing_date"] == date(2019, 11, 29)
    assert holding["published_date"] is None
    assert holding["product_type"] is None
    assert holding["fields"]["ACCESSION_NUMBER"] == ACC
    assert holding["fields"]["SERIES_ID"] == "S000012345"
    assert "public_dissemination_unverified" in holding["issues"]
    assert "fund_classification_unknown" in holding["issues"]


def test_exact_decimal_precision_preserves_raw_strings_and_leading_zero_ids(tmp_path):
    rows = list(nport().iter_nport_rows(archive(tmp_path), tables=("FUND_REPORTED_INFO", "FUND_REPORTED_HOLDING")))
    fund = rows[0]
    holding = rows[1]
    assert fund["net_assets"] == Decimal("100.000000000001")
    assert holding["quantity"] == Decimal("123456789012345678901234.123456789012")
    assert holding["value"] == Decimal("99.000000000001")
    assert holding["weight"] == Decimal("12.345678901234")
    assert holding["member_identifier"] == "012345678"
    assert holding["member_name"] == "서울 Issuer"
    assert isinstance(holding["fields"]["BALANCE"], str)


@pytest.mark.parametrize("member", ["../evil.tsv", "/absolute.tsv", "C:/evil.tsv", "nested\\evil.tsv", "ok/../../evil.tsv"])
def test_unsafe_archive_members_are_rejected_even_if_not_selected(tmp_path, member):
    path = archive(tmp_path, extra={member: "payload"})
    if "\\" in member:
        # Windows ZipInfo normalizes backslashes when creating an archive;
        # restore the hostile filename bytes in local and central headers.
        path.write_bytes(path.read_bytes().replace(member.replace("\\", "/").encode(), member.encode()))
    with pytest.raises(ValueError, match="unsafe"):
        list(nport().iter_nport_rows(path, tables=("SUBMISSION",)))


def test_archive_size_and_compression_guards_run_before_parsing(tmp_path):
    path = archive(tmp_path)
    with pytest.raises(ValueError, match="size"):
        list(nport().iter_nport_rows(path, max_member_bytes=1))
    with pytest.raises(ValueError, match="size"):
        list(nport().iter_nport_rows(path, max_total_bytes=1))
    with pytest.raises(ValueError, match="compression"):
        list(nport().iter_nport_rows(path, max_compression_ratio=1))


def test_resume_section_and_offset_reproduce_stable_suffix_without_changing_keys(tmp_path):
    path = archive(tmp_path, holding_count=3)
    full = list(nport().iter_nport_rows(path))
    resumed = list(nport().iter_nport_rows(path, resume_section="FUND_REPORTED_HOLDING", resume_offset=1))
    start = next(i for i, r in enumerate(full) if r["source_section"] == "FUND_REPORTED_HOLDING" and r["source_offset"] == 1)
    assert resumed == full[start:]
    assert len({r["row_key"] for r in full}) == len(full)


def test_null_report_date_and_invalid_numeric_never_become_filing_day_or_zero(tmp_path):
    rows = list(nport().iter_nport_rows(archive(tmp_path, report="", balance="invalid"), tables=("FUND_REPORTED_HOLDING",)))
    assert rows[0]["report_date"] is None
    assert rows[0]["quantity"] is None
    assert rows[0]["fields"]["BALANCE"] == "invalid"
    assert "report_date_missing" in rows[0]["issues"]
    assert "invalid_numeric:BALANCE" in rows[0]["issues"]


def test_optional_identifier_without_accession_is_kept_with_explicit_unresolved_join(tmp_path):
    row = list(nport().iter_nport_rows(archive(tmp_path), tables=("IDENTIFIERS",)))[0]
    assert row["record_kind"] == "nport_holding"
    assert row["member_identifier"] == "US0123456789"
    assert row["report_date"] is None
    assert "identifier_parent_accession_unresolved" in row["issues"]


def test_streaming_never_uses_zip_bulk_read_and_metadata_is_bounded(tmp_path, monkeypatch):
    path = archive(tmp_path, holding_count=10000)
    def forbidden(*args, **kwargs):
        raise AssertionError("ZIP bulk read is forbidden")
    monkeypatch.setattr(zipfile.ZipFile, "read", forbidden)
    iterator = nport().iter_nport_rows(path, tables=("FUND_REPORTED_HOLDING",))
    assert next(iterator)["source_offset"] == 0
    iterator.close()
    with pytest.raises(ValueError, match="metadata"):
        list(nport().iter_nport_rows(path, max_metadata_rows=1))


def test_missing_selected_section_emits_gap_instead_of_claiming_empty_dataset(tmp_path):
    path = archive(tmp_path)
    with zipfile.ZipFile(path) as z:
        files = {m.filename: z.read(m) for m in z.infolist() if m.filename != "FUND_REPORTED_HOLDING.tsv"}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, value in files.items():
            z.writestr(name, value)
    row = list(nport().iter_nport_rows(path, tables=("FUND_REPORTED_HOLDING",)))[0]
    assert row["record_kind"] == "source_gap"
    assert "source_table_missing" in row["issues"]
