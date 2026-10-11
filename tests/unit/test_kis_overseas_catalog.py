from datetime import datetime, timezone
import importlib
import io
import zipfile

import pytest


STAMP = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


def catalog():
    name = "backtest_data.catalog.kis_overseas"
    assert importlib.util.find_spec(name) is not None, "KIS US current master catalog is required"
    return importlib.import_module(name)


def master_row(symbol="AAPL", exchange="NAS", **changes):
    values = ["US", {"NAS": "22", "NYS": "21", "AMS": "23"}[exchange], exchange,
              "거래소", symbol, exchange + symbol, "이름", "EXAMPLE INC", "2", "USD", "4", "",
              "123.4500", "1", "1", "930", "1600", "N", "", "730", "0", "0", "   ", "   "]
    for index, value in changes.items():
        values[int(index)] = value
    return "\t".join(values)


def archive(lines=None, *, exchange="NAS", name=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr(zipfile.ZipInfo(name or exchange + "MST.COD"), "\r\n".join(lines or []).encode("cp949"),
            compress_type=zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


def member(symbol="AAPL", market="US", identity=None):
    return {"market": market, "security_id": identity or market + ":" + symbol,
            "symbol": symbol, "identifier": "nasdaq_directory:" + symbol,
            "venue": "ARCA", "name": "Example", "security_type": "unclassified"}


def captures(*rows):
    grouped = {exchange: [] for exchange in ("NAS", "NYS", "AMS")}
    for symbol, exchange in rows:
        grouped[exchange].extend(catalog().parse_kis_overseas_master(
            archive([master_row(symbol, exchange)], exchange=exchange), exchange))
    return [{"exchange": exchange, "rows": values, "raw_id": exchange + "-raw",
             "hash": str(index) * 64, "captured_at": STAMP.isoformat(),
             "url": "https://new.real.download.dws.co.kr/common/master/" + exchange.lower() + "mst.cod.zip"}
            for index, (exchange, values) in enumerate(grouped.items(), 1)]


def test_parser_preserves_exact_provider_fields_without_binary_floats():
    result = catalog().parse_kis_overseas_master(archive([master_row("BRK/B", "NYS")], exchange="NYS"), "NYS")
    assert len(result) == 1
    row = result[0]
    assert row.provider_symbol == "BRK/B"
    assert row.master_realtime_symbol == "NYSBRK/B"
    assert row.realtime_symbol == "DNYSBRK/B"
    assert row.fields["base_price"] == "123.4500"
    assert row.fields["exchange_name_ko"] == "거래소"
    assert row.fields["tick_details"] == "   "
    assert len(row.fields) == 24


@pytest.mark.parametrize("content", [b"not a zip", archive(), archive([master_row()], name="../NASMST.COD"),
    archive([master_row()], name="OTHER.COD"), archive([master_row() + "\textra"]),
    archive([master_row(**{"0": "KR"})]), archive([master_row(**{"1": "21"})]),
    archive([master_row(**{"5": "NYSAPPLE"})]), archive([master_row(**{"9": "EUR"})]),
    archive([master_row(**{"10": "4.0"})]), archive([master_row(**{"4": " AAPL"})]),
    archive([master_row(), master_row()])])
def test_parser_rejects_unsafe_changed_empty_and_duplicate_master_rows(content):
    with pytest.raises(ValueError):
        catalog().parse_kis_overseas_master(content, "NAS")


def test_parser_rejects_multiple_members_and_oversized_expansion():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        output.writestr("NASMST.COD", master_row())
        output.writestr("unexpected.txt", "extra")
    with pytest.raises(ValueError):
        catalog().parse_kis_overseas_master(buffer.getvalue(), "NAS")
    oversized = bytearray(archive([master_row()]))
    # Declared expanded length is checked before any member read/decompression.
    central = oversized.index(b"PK\x01\x02")
    oversized[central + 24:central + 28] = (33 * 1024 * 1024).to_bytes(4, "little")
    with pytest.raises(ValueError):
        catalog().parse_kis_overseas_master(bytes(oversized), "NAS")


def test_unknown_zip_compression_is_reported_as_invalid_master():
    content = bytearray(archive([master_row()]))
    central = content.index(b"PK\x01\x02")
    content[8:10] = (99).to_bytes(2, "little")
    content[central + 10:central + 12] = (99).to_bytes(2, "little")
    with pytest.raises(ValueError):
        catalog().parse_kis_overseas_master(bytes(content), "NAS")


def test_exact_mapping_retains_raw_proof_and_never_guesses_target_venue_or_punctuation():
    members = [member("AAPL"), member("BRK.B"), member("BRK/B"), member("AAPL", "KR")]
    result = catalog().resolve_kis_overseas_mappings(members, captures(("AAPL", "NAS"), ("BRK/B", "NYS")))
    assert set(result) == {"US:AAPL", "US:BRK.B", "US:BRK/B"}
    proof = result["US:AAPL"]["mapping_evidence"]
    assert proof["market"] == "US"
    assert proof["security_id"] == "US:AAPL"
    assert proof["qualified_current_mapping"] is True
    assert proof["exchange"] == "NAS"  # Directory ARCA does not guess KIS EXCD.
    assert proof["master_raw_id"] == "NAS-raw"
    assert proof["master_hash"] == "1" * 64
    assert proof["master_realtime_symbol"] == "NASAAPL"
    assert proof["realtime_symbol"] == "DNASAAPL"
    assert proof["realtime_symbol_derivation"] == "D + master_realtime_symbol"
    assert proof["historical_identity_verified"] is False
    assert result["US:BRK.B"]["reason"] == "kis_overseas_mapping_unavailable"
    assert result["US:BRK/B"]["mapping_evidence"]["provider_symbol"] == "BRK/B"


def test_symbol_on_multiple_provider_exchanges_or_multiple_target_ids_is_ambiguous():
    result = catalog().resolve_kis_overseas_mappings([member()], captures(("AAPL", "NAS"), ("AAPL", "NYS")))
    assert result["US:AAPL"]["reason"] == "kis_overseas_mapping_ambiguous"
    assert result["US:AAPL"]["candidate_exchanges"] == ["NAS", "NYS"]
    result = catalog().resolve_kis_overseas_mappings(
        [member(identity="one"), member(identity="two")], captures(("AAPL", "NAS")))
    assert all(item["reason"] == "kis_overseas_mapping_ambiguous" for item in result.values())


def test_incomplete_master_set_cannot_certify_global_symbol_uniqueness():
    result = catalog().resolve_kis_overseas_mappings([member()], captures(("AAPL", "NAS"))[:1])
    assert result["US:AAPL"]["reason"] == "kis_overseas_master_incomplete"
    assert result["US:AAPL"].get("mapping_evidence") is None
