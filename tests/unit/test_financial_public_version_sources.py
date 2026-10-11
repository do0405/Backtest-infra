"""Exact originals and full SQL inventories qualify; labels never suffice."""
from dataclasses import asdict, replace
from datetime import date
import hashlib
import gzip
from io import BytesIO
import json
import tarfile
from types import SimpleNamespace
from zipfile import ZipFile, ZipInfo

import pytest

from backtest_data.fundamentals.xbrl import parse_xbrl


DAY = date(2024, 3, 1)
ACCESSION = "0000950170-24-012345"  # Filing agent prefix differs from issuer.
FEED_URL = "https://www.sec.gov/Archives/edgar/Feed/2024/QTR1/20240301.nc.tar.gz"
RECEIPT = "20240301001234"
DART_URL = "https://opendart.fss.or.kr/api/fnlttXbrl.xml"
LIST_URL = "https://opendart.fss.or.kr/api/list.json"


def instance(*, context="C", unit="U", prefix="a", value="100", entity="320193",
             scheme="http://www.sec.gov/CIK", decimals="-3", extra="", dimension=""):
    return f'''<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:{prefix}="urn:accounts"
        xmlns:iso="http://www.xbrl.org/2003/iso4217" xmlns:xbrldi="http://xbrl.org/2006/xbrldi">
      <context id="{context}"><entity><identifier scheme="{scheme}">{entity}</identifier>{dimension}</entity>
        <period><instant>2023-12-31</instant></period></context>
      <unit id="{unit}"><measure>iso:USD</measure></unit>
      <{prefix}:Cash id="cash" contextRef="{context}" unitRef="{unit}" decimals="{decimals}">{value}</{prefix}:Cash>
      <{prefix}:Assets id="assets" contextRef="{context}" unitRef="{unit}" decimals="{decimals}">900</{prefix}:Assets>{extra}
    </xbrl>'''.encode()


def sql_document(body, *, source="sec", content_hash=None, form="10-K"):
    parsed = parse_xbrl(body)
    inventory = {"counts": asdict(parsed.inventory), "contexts": {key: asdict(ctx) for key, ctx in parsed.contexts.items()},
                 "units": parsed.units, "issues": [asdict(issue) for issue in parsed.issues],
                 "source_format_complete": parsed.complete}
    filing = SimpleNamespace(id="F", issuer_id="I", source=source,
                             accession=ACCESSION if source == "sec" else RECEIPT, form=form)
    document = SimpleNamespace(id="D", filing_id="F", content_hash=content_hash or hashlib.sha256(body).hexdigest(),
                               extraction_complete=parsed.complete, inventory=inventory,
                               instance_key="0", parser_version=parsed.parser_version,
                               source_url="https://www.sec.gov/Archives/edgar/data/320193/000095017024012345/generated_htm.xml")
    rows = []
    for fact in parsed.facts:
        fields = asdict(fact)
        fields.update(document_id="D", issuer_id="I", evidence={**fact.evidence, "taxonomy": fact.taxonomy,
            "precision": fact.precision, "sign": fact.sign, "format": fact.format,
            "parse_status": fact.parse_status, "source_node_ids": list(fact.source_node_ids)})
        rows.append(SimpleNamespace(**fields))
    return document, filing, rows


def complete_filing(body, *, accession=ACCESSION, cik="0000320193", form="10-K", filed="20240301",
                    filename="original.xml", sgml=False):
    if sgml:
        header = f"<SUBMISSION>\n<ACCESSION-NUMBER>{accession}\n<TYPE>{form}\n<FILING-DATE>{filed}\n<FILER>\n<COMPANY-DATA>\n<CIK>{cik}\n</COMPANY-DATA>\n</FILER>\n"
        ending = b"</SUBMISSION>\n"
    else:
        header = f"<SEC-DOCUMENT>{accession}.txt : 20240301\n<SEC-HEADER>\nACCESSION NUMBER: {accession}\nCONFORMED SUBMISSION TYPE: {form}\nFILED AS OF DATE: {filed}\nFILER:\n COMPANY DATA:\n CENTRAL INDEX KEY: {cik}\n</SEC-HEADER>\n"
        ending = b"</SEC-DOCUMENT>\n"
    return header.encode() + f"<DOCUMENT>\n<TYPE>EX-101.INS\n<SEQUENCE>1\n<FILENAME>{filename}\n<TEXT>\n".encode() + body + b"\n</TEXT>\n</DOCUMENT>\n" + ending


def tar_archive(entries):
    out = BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        for name, body, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            if kind == tarfile.REGTYPE:
                member.size = len(body)
                archive.addfile(member, BytesIO(body))
            else:
                member.linkname = "target.xml"
                archive.addfile(member)
    return out.getvalue()


def verify_sec(body=None, *, original=None, archive=None, header=None, **kwargs):
    from backtest_data.fundamentals.public_version_sources import verify_sec_dated_feed
    body = body or instance()
    document, filing, facts = sql_document(body)
    if archive is None:
        archive = tar_archive([(ACCESSION + ".nc", complete_filing(original or body, **(header or {})), tarfile.REGTYPE)])
    return verify_sec_dated_feed(document=document, filing=filing, sql_facts=facts,
        archive_bytes=archive, archive_url=FEED_URL, archive_day=DAY, expected_cik="320193", **kwargs)


def zip_archive(entries):
    out = BytesIO()
    with ZipFile(out, "w") as archive:
        for name, body in entries:
            archive.writestr(name, body)
    return out.getvalue()


def public_list(**overrides):
    item = {"corp_code": "00123456", "rcept_no": RECEIPT, "rcept_dt": "20240301",
            "report_nm": "사업보고서 (2023.12)"}
    item.update(overrides)
    return json.dumps({"status": "000", "list": [item]}, ensure_ascii=False).encode()


def verify_dart(*, archive=None, list_body=None, original_url=DART_URL, **kwargs):
    from backtest_data.fundamentals.public_version_sources import verify_dart_receipt_original
    body = instance(entity="00123456", scheme="opendart.corp_code")
    archive = archive or zip_archive([("report.xml", body)])
    document, filing, facts = sql_document(body, source="opendart", content_hash=hashlib.sha256(archive).hexdigest(),
                                         form="사업보고서 (2023.12)")
    document.inventory["source_request_metadata"] = {"rcept_no": RECEIPT, "reprt_code": "11011"}
    return verify_dart_receipt_original(document=document, filing=filing, sql_facts=facts,
        archive_bytes=archive, original_url=original_url, public_list_body=list_body or public_list(),
        public_list_url=LIST_URL, expected_corp_code="00123456", requested_receipt=RECEIPT, **kwargs)


@pytest.mark.parametrize("sgml", [False, True])
def test_sec_actual_dated_feed_original_qualifies_even_for_filing_agent_accession(sgml):
    result = verify_sec(header={"sgml": sgml})
    assert result.status == "qualified" and result.public_day == DAY
    proof = result.evidence["proof"]
    assert proof["membership_verified"] and proof["original_version_verified"]
    assert proof["member_hash"] == proof["original_body_hash"]
    assert result.evidence["numeric_binding"]["fact_count"] == 2
    assert result.evidence["numeric_binding"]["sql_digest"] == result.evidence["numeric_binding"]["original_digest"]


def test_sec_generated_filename_requires_complete_semantics_and_structural_context_mapping():
    original = instance(context="opaque", unit="dollars", prefix="different")
    result = verify_sec(original=original)
    assert result.status == "qualified"
    assert result.evidence["proof"]["version_match"] == "complete_semantic_inventory"
    assert result.evidence["context_mapping"] == {"C": ["opaque"]}


@pytest.mark.parametrize("change", [
    {"value": "101"}, {"decimals": "-2"}, {"entity": "999"},
    {"scheme": "urn:other"},
    {"dimension": '<segment><xbrldi:explicitMember dimension="a:Axis">a:Member</xbrldi:explicitMember></segment>'},
    {"extra": '<a:Hidden id="hidden" contextRef="C" unitRef="U" decimals="-3">1</a:Hidden>'},
])
def test_sec_rejects_any_full_inventory_value_precision_entity_dimension_or_count_difference(change):
    assert verify_sec(original=instance(**change)).status == "unavailable"


@pytest.mark.parametrize("header", [
    {"accession": "0000950170-24-999999"}, {"cik": "0000000999"},
    {"form": "10-K/A"}, {"filed": "20240304"},
])
def test_sec_header_identity_and_future_filed_date_are_not_public_proof(header):
    assert verify_sec(header=header).status == "unavailable"


def test_sec_old_filed_date_uses_actual_later_archive_public_day():
    result = verify_sec(header={"filed": "20240229"})
    assert result.status == "qualified" and result.public_day == DAY
    assert result.evidence["proof"]["later_dissemination"] is True


@pytest.mark.parametrize("name,kind", [("../escape.nc", tarfile.REGTYPE), ("/absolute.nc", tarfile.REGTYPE),
    ("C:/absolute.nc", tarfile.REGTYPE), ("link.nc", tarfile.SYMTYPE), ("hard.nc", tarfile.LNKTYPE)])
def test_sec_validates_every_tar_member_even_after_matched_original(name, kind):
    archive = tar_archive([(ACCESSION + ".nc", complete_filing(instance()), tarfile.REGTYPE), (name, b"x", kind)])
    result = verify_sec(archive=archive)
    assert result.status == "unavailable" and result.reason.startswith("unsafe_archive:")


@pytest.mark.parametrize("override", [
    {"max_compressed_bytes": 1}, {"max_decompressed_bytes": 512}, {"max_member_bytes": 1}, {"max_members": 1},
])
def test_sec_archive_limits_bound_compression_expansion_members_and_counts(override):
    from backtest_data.fundamentals.public_version_sources import ArchiveLimits
    archive = tar_archive([(ACCESSION + ".nc", complete_filing(instance()), tarfile.REGTYPE), ("other.nc", b"x", tarfile.REGTYPE)])
    assert verify_sec(archive=archive, limits=ArchiveLimits(**override)).reason.startswith("unsafe_archive:")


@pytest.mark.parametrize("url", [
    "https://www.sec.gov/Archives/edgar/data/320193/original.xml",
    "https://www.sec.gov/Archives/edgar/Feed/2024/QTR2/20240301.nc.tar.gz",
    "https://www.sec.gov/Archives/edgar/Feed/2024/QTR1/20240302.nc.tar.gz",
    "https://evil.example/Archives/edgar/Feed/2024/QTR1/20240301.nc.tar.gz",
])
def test_sec_rejects_non_distribution_or_mismatched_date_url(url):
    from backtest_data.fundamentals.public_version_sources import verify_sec_dated_feed
    document, filing, facts = sql_document(instance())
    result = verify_sec_dated_feed(document=document, filing=filing, sql_facts=facts,
        archive_bytes=tar_archive([(ACCESSION + ".nc", complete_filing(instance()), tarfile.REGTYPE)]),
        archive_url=url, archive_day=DAY, expected_cik="320193")
    assert result.status == "unavailable"


def test_sec_filing_date_acceptance_and_standalone_xml_have_no_distribution_membership():
    archive = tar_archive([("generated_htm.xml", instance(), tarfile.REGTYPE)])
    assert verify_sec(archive=archive).status == "unavailable"


def test_sql_subset_never_qualifies_even_if_every_selected_metric_matches():
    from backtest_data.fundamentals.public_version_sources import verify_sec_dated_feed
    document, filing, facts = sql_document(instance())
    result = verify_sec_dated_feed(document=document, filing=filing, sql_facts=facts[:1],
        archive_bytes=tar_archive([(ACCESSION + ".nc", complete_filing(instance()), tarfile.REGTYPE)]),
        archive_url=FEED_URL, archive_day=DAY, expected_cik="320193")
    assert result.status == "unavailable" and result.reason.startswith("sql_inventory:")


def test_dart_receipt_specific_zip_and_full_public_list_identity_qualify():
    result = verify_dart()
    assert result.status == "qualified" and result.public_day == DAY
    assert result.evidence["proof"]["receipt_specific_original"] is True
    assert result.evidence["proof"]["kind"] == "dart_receipt_original"


@pytest.mark.parametrize("overrides", [
    {"corp_code": "99999999"}, {"rcept_no": "20240301009999"}, {"rcept_dt": "2024-03-01"},
    {"report_nm": "[정정]사업보고서 (2023.12)"},
])
def test_dart_public_list_must_match_company_receipt_day_and_title(overrides):
    assert verify_dart(list_body=public_list(**overrides)).status == "unavailable"


def test_dart_latest_viewer_url_cannot_prove_receipt_original():
    assert verify_dart(original_url="https://dart.fss.or.kr/dsaf001/main.do").status == "unavailable"


def test_dart_latest_json_matching_receipt_is_not_archived_original():
    from backtest_data.fundamentals.public_version_sources import verify_dart_receipt_original
    result = verify_dart_receipt_original(document=SimpleNamespace(id="D"), filing=SimpleNamespace(id="F"),
        sql_facts=[], archive_bytes=public_list(), original_url=DART_URL, public_list_body=public_list(),
        public_list_url=LIST_URL, expected_corp_code="00123456", requested_receipt=RECEIPT)
    assert result.status == "unavailable"


@pytest.mark.parametrize("name", ["../report.xml", "/report.xml", "C:\\report.xml"])
def test_dart_unsafe_zip_paths_fail_closed(name):
    assert verify_dart(archive=zip_archive([(name, instance())])).reason.startswith("unsafe_archive:")


def test_dart_missing_requested_receipt_metadata_is_not_receipt_specific():
    from backtest_data.fundamentals.public_version_sources import verify_dart_receipt_original
    body = instance(entity="00123456", scheme="opendart.corp_code")
    archive = zip_archive([("report.xml", body)])
    document, filing, facts = sql_document(body, source="opendart", content_hash=hashlib.sha256(archive).hexdigest(), form="사업보고서 (2023.12)")
    result = verify_dart_receipt_original(document=document, filing=filing, sql_facts=facts,
        archive_bytes=archive, original_url=DART_URL, public_list_body=public_list(),
        public_list_url=LIST_URL, expected_corp_code="00123456", requested_receipt="20240302001234")
    assert result.status == "unavailable"


@pytest.mark.parametrize("replacement", [
    lambda body: body.replace(b"iso:USD", b"iso:EUR"),
    lambda body: body.replace(b"2023-12-31", b"2022-12-31"),
    lambda body: body.replace(b">100</a:Cash>", b' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"></a:Cash>'),
    lambda body: body.replace(b'</xbrl>', b'<a:Cash id="duplicate" contextRef="C" unitRef="U" decimals="-3">100</a:Cash></xbrl>'),
])
def test_full_original_units_period_nil_and_duplicate_node_multiplicity_must_match(replacement):
    assert verify_sec(original=replacement(instance())).status == "unavailable"


def test_unsupported_original_numeric_node_never_qualifies_as_matched_metrics():
    body = instance(extra='<a:Bad id="bad" contextRef="C" unitRef="U">?</a:Bad>')
    assert verify_sec(original=body).status == "unavailable"


def test_conflicting_original_numbers_never_qualify_with_sampling():
    body = instance(extra='<a:Cash id="other" contextRef="C" unitRef="U" decimals="-3">2000</a:Cash>')
    assert verify_sec(original=body).status == "unavailable"


def test_multiple_matching_semantic_originals_fail_closed_when_original_body_hash_is_unknown():
    body = instance(context="renamed")
    complete = complete_filing(body) + b''
    second = b'<DOCUMENT>\n<TYPE>EX-101.INS\n<SEQUENCE>2\n<FILENAME>second.xml\n<TEXT>\n' + body + b'\n</TEXT>\n</DOCUMENT>\n'
    complete = complete.replace(b'</SEC-DOCUMENT>', second + b'</SEC-DOCUMENT>')
    archive = tar_archive([(ACCESSION + '.nc', complete, tarfile.REGTYPE)])
    assert verify_sec(archive=archive).reason == "original_inventory:ambiguous_original_member"


@pytest.mark.parametrize("entry_type", [tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME])
def test_unsupported_tar_extended_metadata_rejected_before_body_allocation(entry_type):
    archive = tar_archive([(ACCESSION + ".nc", complete_filing(instance()), tarfile.REGTYPE),
                           ("metadata", b"x", entry_type)])
    assert verify_sec(archive=archive).reason == "unsafe_archive:nonregular_member"


def test_dart_zip_symlink_is_rejected_even_when_matching_original_precedes_it():
    out = BytesIO()
    body = instance(entity="00123456", scheme="opendart.corp_code")
    with ZipFile(out, "w") as archive:
        archive.writestr("report.xml", body)
        entry = ZipInfo("link.xml")
        entry.create_system = 3
        entry.external_attr = (0o120777 << 16)
        archive.writestr(entry, "report.xml")
    assert verify_dart(archive=out.getvalue()).reason == "unsafe_archive:nonregular_or_encrypted_member"


@pytest.mark.parametrize("override", [
    {"max_compressed_bytes": 1}, {"max_decompressed_bytes": 1}, {"max_member_bytes": 1}, {"max_members": 1},
])
def test_dart_compressed_expansion_member_and_count_limits(override):
    from backtest_data.fundamentals.public_version_sources import ArchiveLimits
    body = instance(entity="00123456", scheme="opendart.corp_code")
    assert verify_dart(archive=zip_archive([("report.xml", body), ("notes.txt", b"x")]),
                       limits=ArchiveLimits(**override)).reason.startswith("unsafe_archive:")


def test_dart_list_day_must_agree_with_authoritative_receipt_date_identity():
    assert verify_dart(list_body=public_list(rcept_dt="20240304")).status == "unavailable"


def test_dart_zip_central_directory_count_bound_is_checked_before_zipfile_parses_entries(monkeypatch):
    import backtest_data.fundamentals.public_version_sources as verifier
    archive = bytearray(zip_archive([("report.xml", b"x")]))
    end = archive.rfind(b"PK\x05\x06")
    archive[end + 8:end + 12] = (60000).to_bytes(2, "little") * 2
    monkeypatch.setattr(verifier, "ZipFile", lambda *_args, **_kwargs: pytest.fail("unbounded ZIP entry parse"))
    assert verify_dart(archive=bytes(archive)).reason == "unsafe_archive:member_count_limit"


@pytest.mark.parametrize("entity,scheme", [("99999999", "opendart.corp_code"), ("00123456", "urn:unknown-entity")])
def test_dart_sql_and_original_with_same_wrong_or_unbound_company_context_never_qualify(entity, scheme):
    from backtest_data.fundamentals.public_version_sources import verify_dart_receipt_original
    body = instance(entity=entity, scheme=scheme)
    archive = zip_archive([("report.xml", body)])
    document, filing, facts = sql_document(body, source="opendart", content_hash=hashlib.sha256(archive).hexdigest(),
                                         form="사업보고서 (2023.12)")
    result = verify_dart_receipt_original(document=document, filing=filing, sql_facts=facts,
        archive_bytes=archive, original_url=DART_URL, public_list_body=public_list(),
        public_list_url=LIST_URL, expected_corp_code="00123456", requested_receipt=RECEIPT)
    assert result.status == "unavailable" and result.reason == "original_inventory:issuer_context_mismatch"


def oversized_unrelated_submission():
    return complete_filing(b"Unrelated financial note\n" * 2000, accession="0000950170-24-999999")


def test_sec_streams_past_oversized_unrelated_submission_without_retaining_its_body(monkeypatch):
    import backtest_data.fundamentals.public_version_sources as verifier
    target = complete_filing(instance())
    unrelated = oversized_unrelated_submission()
    limits = verifier.ArchiveLimits(max_member_bytes=len(target), max_header_bytes=512)
    original_exact = verifier._BoundedGzip.exact
    sizes = []
    def bounded_exact(reader, size):
        sizes.append(size)
        assert size <= limits.max_member_bytes
        return original_exact(reader, size)
    monkeypatch.setattr(verifier._BoundedGzip, "exact", bounded_exact)
    archive = tar_archive([("other.nc", unrelated, tarfile.REGTYPE), (ACCESSION + ".nc", target, tarfile.REGTYPE)])
    result = verify_sec(archive=archive, limits=limits)
    assert result.status == "qualified"
    assert max(sizes) < len(unrelated)


def test_sec_matching_target_submission_keeps_strict_member_body_size_limit():
    from backtest_data.fundamentals.public_version_sources import ArchiveLimits
    target = complete_filing(instance())
    limits = ArchiveLimits(max_member_bytes=len(target) - 1, max_header_bytes=512)
    assert verify_sec(limits=limits).reason == "unsafe_archive:member_size_limit"


def test_sec_identifies_actual_accession_from_header_without_filename_membership_guess():
    archive = tar_archive([("renamed-original.nc", complete_filing(instance()), tarfile.REGTYPE)])
    result = verify_sec(archive=archive)
    assert result.status == "qualified"
    assert result.evidence["proof"]["member_name"].startswith("renamed-original.nc::")


def test_sec_matching_filename_with_unrelated_header_is_drained_and_actual_target_header_is_found():
    from backtest_data.fundamentals.public_version_sources import ArchiveLimits
    target = complete_filing(instance())
    archive = tar_archive([(ACCESSION + ".nc", oversized_unrelated_submission(), tarfile.REGTYPE),
                           ("actual-target.nc", target, tarfile.REGTYPE)])
    result = verify_sec(archive=archive, limits=ArchiveLimits(max_member_bytes=len(target), max_header_bytes=512))
    assert result.status == "qualified"
    assert result.evidence["proof"]["member_name"].startswith("actual-target.nc::")


@pytest.mark.parametrize("name,reason", [("../other.nc", "unsafe_archive:entry_path"),
    ("duplicate.nc", "unsafe_archive:duplicate_member")])
def test_oversized_unrelated_members_still_check_paths_and_duplicate_tar_names(name, reason):
    from backtest_data.fundamentals.public_version_sources import ArchiveLimits
    target, unrelated = complete_filing(instance()), oversized_unrelated_submission()
    entries = [(ACCESSION + ".nc", target, tarfile.REGTYPE)]
    if name == "duplicate.nc":
        entries.append((name, unrelated, tarfile.REGTYPE))
    entries.append((name, unrelated, tarfile.REGTYPE))
    result = verify_sec(archive=tar_archive(entries), limits=ArchiveLimits(max_member_bytes=len(target), max_header_bytes=512))
    assert result.reason == reason


def test_streamed_unrelated_body_still_counts_against_total_decompressed_budget():
    from backtest_data.fundamentals.public_version_sources import ArchiveLimits
    target = complete_filing(instance())
    archive = tar_archive([(ACCESSION + ".nc", target, tarfile.REGTYPE),
                           ("other.nc", oversized_unrelated_submission(), tarfile.REGTYPE)])
    result = verify_sec(archive=archive, limits=ArchiveLimits(max_member_bytes=len(target),
        max_header_bytes=512, max_decompressed_bytes=8192))
    assert result.reason == "unsafe_archive:decompressed_size_limit"


def test_oversized_unrelated_drain_does_not_skip_invalid_tar_trailing_data():
    from backtest_data.fundamentals.public_version_sources import ArchiveLimits
    target = complete_filing(instance())
    archive = tar_archive([(ACCESSION + ".nc", target, tarfile.REGTYPE),
                           ("other.nc", oversized_unrelated_submission(), tarfile.REGTYPE)])
    archive = gzip.compress(gzip.decompress(archive) + b"invalid trailing bytes")
    result = verify_sec(archive=archive, limits=ArchiveLimits(max_member_bytes=len(target), max_header_bytes=512))
    assert result.reason == "unsafe_archive:trailing_tar_data"


def test_matching_target_header_identity_is_verified_before_retaining_body(monkeypatch):
    import backtest_data.fundamentals.public_version_sources as verifier
    target = complete_filing(instance(), cik="999")
    original_exact = verifier._BoundedGzip.exact
    def only_prefix_reads(reader, size):
        assert size <= 576, "target body retained before header issuer verification"
        return original_exact(reader, size)
    monkeypatch.setattr(verifier._BoundedGzip, "exact", only_prefix_reads)
    archive = tar_archive([("whatever.nc", target, tarfile.REGTYPE)])
    result = verify_sec(archive=archive, limits=verifier.ArchiveLimits(max_header_bytes=512))
    assert result.reason == "sec_header:issuer_cik_mismatch"


def test_oversized_unrelated_drain_still_checks_gzip_crc_after_matched_target():
    from backtest_data.fundamentals.public_version_sources import ArchiveLimits
    target = complete_filing(instance())
    archive = bytearray(tar_archive([(ACCESSION + ".nc", target, tarfile.REGTYPE),
                                    ("other.nc", oversized_unrelated_submission(), tarfile.REGTYPE)]))
    archive[-8] ^= 255
    result = verify_sec(archive=bytes(archive), limits=ArchiveLimits(max_member_bytes=len(target), max_header_bytes=512))
    assert result.reason == "unsafe_archive:invalid_gzip"


def test_unrelated_submission_with_unbounded_header_cannot_hide_another_matching_accession():
    from backtest_data.fundamentals.public_version_sources import ArchiveLimits
    target = complete_filing(instance())
    hidden = b"<SEC-HEADER>\n" + b"COMMENT\n" * 200 + b"ACCESSION NUMBER: " + ACCESSION.encode() + b"\n"
    archive = tar_archive([(ACCESSION + ".nc", target, tarfile.REGTYPE), ("hidden.nc", hidden, tarfile.REGTYPE)])
    result = verify_sec(archive=archive, limits=ArchiveLimits(max_member_bytes=len(target), max_header_bytes=512))
    assert result.reason == "unsafe_archive:submission_header_limit"


def test_stream_drained_unrelated_member_must_have_its_full_declared_body():
    from backtest_data.fundamentals.public_version_sources import ArchiveLimits
    target = complete_filing(instance())
    archive = tar_archive([(ACCESSION + ".nc", target, tarfile.REGTYPE),
                           ("other.nc", oversized_unrelated_submission(), tarfile.REGTYPE)])
    uncompressed = gzip.decompress(archive)
    # Retain the target and the next member header/prefix, truncate its body.
    truncated = uncompressed[:512 + ((len(target) + 511) // 512) * 512 + 512 + 1024]
    result = verify_sec(archive=gzip.compress(truncated), limits=ArchiveLimits(max_member_bytes=len(target), max_header_bytes=512))
    assert result.reason == "unsafe_archive:truncated_tar"
