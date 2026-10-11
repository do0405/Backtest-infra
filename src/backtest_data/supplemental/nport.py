"""Bounded read-only streaming of SEC quarterly N-PORT UTF-8 TSV archives.

Accession and historical SEC series identifiers provide the joins. No current
fund/ticker list filters the records. Filing dates are not dissemination dates.
Optional identifier rows lack accession in the SEC schema and remain explicitly
unjoined rather than constructing a holdings-sized in-memory identity index.
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import io
from pathlib import Path, PurePosixPath
import stat
import sys
from typing import Iterator, Sequence
import zipfile


TABLES = ("SUBMISSION", "REGISTRANT", "FUND_REPORTED_INFO", "FUND_REPORTED_HOLDING", "IDENTIFIERS")
METADATA_TABLES = TABLES[:3]
GIB = 1024 ** 3
SEC_DATASET_URL = "https://www.sec.gov/data-research/sec-markets-data/form-n-port-data-sets"
SEC_README_URL = "https://www.sec.gov/files/nport_readme.pdf"


def _archive_members(archive: zipfile.ZipFile, *, max_member_bytes: int, max_total_bytes: int,
                     max_compression_ratio: int) -> dict[str, zipfile.ZipInfo]:
    members, total = {}, 0
    infos = archive.infolist()
    if len(infos) > 1000:
        raise ValueError("archive member count exceeds bounded limit")
    for info in infos:
        # ZipInfo normalizes Windows separators in filename; orig_filename
        # retains the central-directory spelling needed for safety checks.
        name = info.orig_filename
        path = PurePosixPath(name)
        if (not name or "\\" in name or ":" in name or "\x00" in name or path.is_absolute()
                or any(p in (".", "..") for p in name.split("/"))
                or stat.S_ISLNK(info.external_attr >> 16)):
            raise ValueError("unsafe ZIP member path or symlink")
        if info.flag_bits & 1:
            raise ValueError("unsafe encrypted ZIP member")
        if info.is_dir():
            continue
        if info.file_size > max_member_bytes:
            raise ValueError("ZIP member size exceeds bounded limit")
        total += info.file_size
        if total > max_total_bytes:
            raise ValueError("ZIP total size exceeds bounded limit")
        if info.file_size > max_compression_ratio * max(info.compress_size, 1):
            raise ValueError("ZIP compression ratio exceeds bounded limit")
        table = path.stem.upper() if path.suffix.lower() in (".tsv", ".txt") else None
        if table in TABLES:
            if table in members:
                raise ValueError("ambiguous duplicate ZIP table member")
            members[table] = info
    return members


def _bounded_lines(stream: io.TextIOWrapper, *, max_line_chars: int = 1024 * 1024):
    while True:
        line = stream.readline(max_line_chars + 1)
        if not line:
            return
        if len(line) > max_line_chars:
            raise ValueError("TSV line size exceeds bounded limit")
        yield line


def _iter_table(archive: zipfile.ZipFile, member: zipfile.ZipInfo, section: str) -> Iterator[tuple[int, dict[str, str]]]:
    with archive.open(member, "r") as binary, io.TextIOWrapper(binary, encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(_bounded_lines(stream), delimiter="\t", strict=True)
        try:
            headers = next(reader)
        except StopIteration:
            raise ValueError(f"empty TSV table without header: {section}")
        if not headers or len(headers) != len(set(headers)):
            raise ValueError(f"invalid duplicate TSV headers: {section}")
        required = {"HOLDING_ID", "IDENTIFIERS_ID"} if section == "IDENTIFIERS" else {"ACCESSION_NUMBER"}
        if section == "FUND_REPORTED_HOLDING":
            required.add("HOLDING_ID")
        if not required.issubset(headers):
            raise ValueError(f"missing required TSV headers: {section}")
        offset = 0
        for cells in reader:
            if not cells:
                continue
            if len(cells) != len(headers):
                raise ValueError(f"TSV column count mismatch: {section} row {offset}")
            yield offset, dict(zip(headers, cells))
            offset += 1


def _source_date(value: str | None, field: str, issues: list[str]) -> date | None:
    if value is None or value.strip().upper() in ("", "NULL", "\\N"):
        issues.append(field.lower() + "_missing")
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d-%b-%Y", "%d-%b-%y", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    issues.append("invalid_date:" + field)
    return None


def _number(raw: dict, field: str, issues: list[str]) -> Decimal | None:
    value = raw.get(field)
    if value is None or str(value).strip().upper() in ("", "NULL", "\\N"):
        issues.append("numeric_missing:" + field)
        return None
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError):
        issues.append("invalid_numeric:" + field)
        return None
    if not result.is_finite():
        issues.append("invalid_numeric:" + field)
        return None
    return result


def _empty_row(kind: str, section: str, offset: int, entity: str, row_key: str, fields: dict, issues: list) -> dict:
    row = {name: None for name in ("entity_name", "member_identifier", "member_name", "product_type", "currency",
        "report_date", "filing_date", "published_date", "effective_date", "ex_date", "record_date", "pay_date",
        "quantity", "value", "weight", "nav", "net_assets", "total_assets", "total_liabilities", "fee_rate")}
    row.update(record_kind=kind, entity_key=entity, row_key=row_key, source_section=section, source_offset=offset,
               fields=fields, issues=issues, date_basis="sec_report_date_public_dissemination_unverified")
    return row


def _normalize(section: str, offset: int, raw: dict[str, str], indices: dict[str, dict]) -> dict:
    accession = raw.get("ACCESSION_NUMBER", "")
    submission = indices["SUBMISSION"].get(accession, {})
    registrant = indices["REGISTRANT"].get(accession, {})
    fund = indices["FUND_REPORTED_INFO"].get(accession, {})
    issues = ["public_dissemination_unverified", "fund_classification_unknown"]
    fields = dict(raw)
    for metadata in (submission, registrant, fund):
        for key in ("ACCESSION_NUMBER", "REPORT_DATE", "FILING_DATE", "SUB_TYPE", "CIK", "REGISTRANT_NAME",
                    "SERIES_ID", "SERIES_NAME", "SERIES_LEI"):
            if key in metadata:
                fields.setdefault(key, metadata[key])
    fields.update(source_section=section, source_offset=offset)
    series = fund.get("SERIES_ID") or raw.get("SERIES_ID")
    entity = series or accession or "holding:" + raw.get("HOLDING_ID", "unknown")
    key_parts = [section, accession]
    if section == "FUND_REPORTED_HOLDING":
        key_parts.append(raw.get("HOLDING_ID", ""))
    elif section == "IDENTIFIERS":
        key_parts.extend([raw.get("HOLDING_ID", ""), raw.get("IDENTIFIERS_ID", "")])
    if (section != "IDENTIFIERS" and not accession) or (section in TABLES[3:] and not raw.get("HOLDING_ID")):
        issues.append("primary_key_missing")
        key_parts.append(hashlib.sha256(repr(sorted(raw.items())).encode("utf-8")).hexdigest())
    kind = "nport_filing" if section in TABLES[:2] else "nport_fund" if section == "FUND_REPORTED_INFO" else "nport_holding"
    row = _empty_row(kind, section, offset, entity, ":".join(key_parts), fields, issues)
    row.update(entity_name=fund.get("SERIES_NAME") or raw.get("SERIES_NAME") or registrant.get("REGISTRANT_NAME"),
               report_date=_source_date(submission.get("REPORT_DATE"), "REPORT_DATE", issues),
               filing_date=_source_date(submission.get("FILING_DATE"), "FILING_DATE", issues))
    if accession and not submission:
        issues.append("submission_join_missing")
    if not series:
        issues.append("series_identifier_missing")
    if section == "REGISTRANT":
        row["member_identifier"] = raw.get("CIK") or None
        row["member_name"] = raw.get("REGISTRANT_NAME") or None
    elif section == "FUND_REPORTED_INFO":
        row.update(currency="USD", net_assets=_number(raw, "NET_ASSETS", issues),
                   total_assets=_number(raw, "TOTAL_ASSETS", issues), total_liabilities=_number(raw, "TOTAL_LIABILITIES", issues))
    elif section == "FUND_REPORTED_HOLDING":
        row.update(member_identifier=raw.get("ISSUER_CUSIP") or raw.get("ISSUER_LEI") or None,
                   member_name=raw.get("ISSUER_NAME") or None, currency=raw.get("CURRENCY_CODE") or None,
                   quantity=_number(raw, "BALANCE", issues), value=_number(raw, "CURRENCY_VALUE", issues),
                   weight=_number(raw, "PERCENTAGE", issues))
        fields.update(quantity_unit=raw.get("UNIT", ""), weight_unit="percent_of_net_assets", value_unit="source_CURRENCY_VALUE")
    elif section == "IDENTIFIERS":
        row["member_identifier"] = raw.get("IDENTIFIER_ISIN") or raw.get("IDENTIFIER_TICKER") or raw.get("OTHER_IDENTIFIER") or None
        if not accession:
            issues.append("identifier_parent_accession_unresolved")
    return row


def iter_nport_rows(archive_path: str | Path, *, tables: Sequence[str] = TABLES,
                    resume_section: str | None = None, resume_offset: int = 0,
                    max_member_bytes: int = 8 * GIB, max_total_bytes: int = 32 * GIB,
                    max_compression_ratio: int = 500, max_metadata_rows: int = 2_000_000,
                    max_metadata_bytes: int = 256 * 1024 * 1024) -> Iterator[dict]:
    """Yield common-stage rows in canonical table order without extracting files.

    Offsets count zero-based TSV data records, excluding header and blank lines.
    Resume skips earlier sections and the first ``resume_offset`` records in the
    named section. Stable source primary keys make resumed rows idempotent.
    Three bounded filing-level maps are rebuilt on resume; holdings and optional
    identifiers are always streamed and never materialized as a complete list.
    """
    selected = tuple(t.upper().removesuffix(".TSV") for t in tables)
    if not selected or len(set(selected)) != len(selected) or any(t not in TABLES for t in selected):
        raise ValueError("unknown or duplicate N-PORT table selection")
    section = resume_section.upper().removesuffix(".TSV") if resume_section else None
    if section is not None and section not in selected:
        raise ValueError("resume section is not selected")
    if resume_offset < 0 or (resume_offset and section is None):
        raise ValueError("invalid resume offset")
    if any(v <= 0 for v in (max_member_bytes, max_total_bytes, max_compression_ratio, max_metadata_rows, max_metadata_bytes)):
        raise ValueError("archive and metadata bounds must be positive")
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            members = _archive_members(archive, max_member_bytes=max_member_bytes,
                                       max_total_bytes=max_total_bytes, max_compression_ratio=max_compression_ratio)
            indices = {table: {} for table in METADATA_TABLES}
            metadata_rows = metadata_bytes = 0
            for table in METADATA_TABLES:
                if table not in members:
                    continue
                for _, raw in _iter_table(archive, members[table], table):
                    metadata_rows += 1
                    metadata_bytes += sys.getsizeof(raw) + sum(sys.getsizeof(k) + sys.getsizeof(v) for k, v in raw.items())
                    if metadata_rows > max_metadata_rows or metadata_bytes > max_metadata_bytes:
                        raise ValueError("N-PORT metadata exceeds bounded memory limit")
                    key = raw.get("ACCESSION_NUMBER", "")
                    if not key:
                        continue
                    if key in indices[table]:
                        raise ValueError("duplicate metadata accession is ambiguous")
                    indices[table][key] = raw
            for table in TABLES:
                if table not in selected or (section is not None and TABLES.index(table) < TABLES.index(section)):
                    continue
                if table not in members:
                    yield _empty_row("source_gap", table, 0, "nport:" + Path(archive_path).name,
                                     "missing_table:" + table, {"source_section": table, "source_offset": 0}, ["source_table_missing"])
                    continue
                for offset, raw in _iter_table(archive, members[table], table):
                    if table == section and offset < resume_offset:
                        continue
                    yield _normalize(table, offset, raw, indices)
    except (zipfile.BadZipFile, UnicodeDecodeError, csv.Error, EOFError, NotImplementedError) as exc:
        raise ValueError("invalid or unsupported N-PORT ZIP/TSV archive") from exc
