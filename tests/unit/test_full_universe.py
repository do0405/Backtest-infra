import io
import zipfile

from backtest_data.catalog.universe import parse_us_directory, parse_kis_directory, required_work


def test_entire_directory_includes_etf_etn_and_unknown_types_without_sampling():
    lines = ["Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares"]
    lines += [f"X{i}|Company {i}|Q|N|N|100|N|N" for i in range(150)]
    lines += ["FUND|A fund|Q|N|N|100|Y|N", "NOTE|A note|Q|N|N|100|N|N", "File Creation Time: 0101202010"]
    entries = parse_us_directory("\n".join(lines).encode(), "nasdaq")
    assert len(entries) == 152
    assert next(x for x in entries if x.symbol == "FUND").security_type == "etp_unknown"
    work = required_work(entries, output_start="2016-01-01", output_end="2026-10-07")
    assert {x.symbol for x in entries} == {x["symbol"] for x in work}
    assert all(any(x["symbol"] == item.symbol and x["domain"] == "prices" for x in work) for item in entries)


def test_kis_schema_preserves_leading_zero_and_full_short_code():
    tail = "ST" + " " * 225
    line = "005930   KR7005930003삼성전자" + tail
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("kospi_code.mst", (line + "\n").encode("cp949"))
    entries = parse_kis_directory(archive.getvalue(), "KOSPI")
    assert entries[0].symbol == "005930"
    assert entries[0].identifier == "KR7005930003"
    assert entries[0].name == "삼성전자"


def test_konex_verified_master_layout_preserves_every_row():
    archive = io.BytesIO()
    rows = [f"{i:06}   KR{i:010}종목 {i}" + "ST" + " " * 182 for i in range(125)]
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("konex_code.mst", ("\n".join(rows) + "\n").encode("cp949"))
    entries = parse_kis_directory(archive.getvalue(), "KONEX")
    assert len(entries) == 125
    assert entries[0].venue == "KONEX"
    assert entries[0].symbol == "000000"
    assert len(required_work(entries, output_start="2016-01-01", output_end="2026-10-07")) == 375
