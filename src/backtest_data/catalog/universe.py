import csv
from dataclasses import dataclass, asdict
import io
from pathlib import PurePosixPath
import zipfile


@dataclass(frozen=True)
class UniverseEntry:
    market: str
    venue: str
    symbol: str
    identifier: str
    name: str
    security_type: str
    evidence: dict


def parse_us_directory(content: bytes, directory: str) -> list[UniverseEntry]:
    if directory not in {"nasdaq", "other"}:
        raise ValueError("Unknown directory contract")
    rows = csv.DictReader(io.StringIO(content.decode("utf-8-sig")), delimiter="|")
    result = []
    for row in rows:
        symbol = row.get("Symbol") if directory == "nasdaq" else row.get("ACT Symbol")
        if not symbol or symbol.startswith("File Creation Time") or row.get("Test Issue") == "Y":
            continue
        required = {"Security Name", "Test Issue", "ETF"}
        if not required.issubset(row) or row["Test Issue"] not in {"Y", "N"} or row["ETF"] not in {"Y", "N"}:
            raise ValueError("US directory schema drift")
        venue = "NASDAQ" if directory == "nasdaq" else row.get("Exchange", "unknown")
        # Nasdaq's ETF flag includes product semantics requiring later classification;
        # names alone never prove ETF/ETN/common/preferred/ADR identity.
        kind = "etp_unknown" if row["ETF"] == "Y" else "equity_or_note_unknown"
        result.append(UniverseEntry("US", venue, symbol, f"nasdaq_directory:{symbol}", row["Security Name"], kind, dict(row)))
    if not result:
        raise ValueError("Empty directory cannot certify universe")
    return result


def parse_kis_directory(content: bytes, market: str) -> list[UniverseEntry]:
    # Provider's stocks_info/kis_{market}_code_mst.py fixed character layouts.
    sizes = {"KOSPI": 227, "KOSDAQ": 221, "KONEX": 184}
    if market not in sizes:
        raise ValueError("KIS market schema is unverified")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        name = f"{market.lower()}_code.mst"
        entries = archive.infolist()
        if len(entries) != 1 or entries[0].filename != name or entries[0].file_size > 32 * 1024 * 1024:
            raise ValueError("Unsafe or changed master ZIP schema")
        content_text = archive.read(name).decode("cp949")
    result = []
    for line in content_text.splitlines():
        if len(line) < 21 + sizes[market]:
            raise ValueError("Truncated master row")
        header, tail = line[:-sizes[market]], line[-sizes[market]:]
        symbol, identifier, name = header[:9].strip(), header[9:21].strip(), header[21:].strip()
        if not symbol or not identifier or not name:
            raise ValueError("Master identity missing")
        group = tail[:2]
        result.append(UniverseEntry("KR", market, symbol, identifier, name, "unclassified",
                                    {"group_code": group, "fixed_width_tail": tail, "schema": "kis_master_v1"}))
    if not result:
        raise ValueError("Empty master cannot certify universe")
    return result


def required_work(entries: list[UniverseEntry], *, output_start: str, output_end: str) -> list[dict]:
    result = []
    for item in entries:
        for domain in ("prices", "vwap", "fundamentals"):
            result.append({"market": item.market, "symbol": item.symbol, "identifier": item.identifier,
                           "domain": domain, "output_start": output_start, "output_end": output_end,
                           "warmup_required": domain != "prices", "classification_pending": item.security_type in
                           {"unclassified", "etp_unknown", "equity_or_note_unknown"}})
    return result
