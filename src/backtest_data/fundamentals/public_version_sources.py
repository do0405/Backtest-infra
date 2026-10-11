"""Bounded, memory-only regulator originals and complete SQL version matching.

These pure functions verify bytes returned by the existing regulator clients.
They neither fetch nor persist source bytes. A caller must bind the supplied
bytes to the named, authenticated provider response and the declared request.
Unsupported originals produce an unavailable attempt, never historical proof.
"""
from dataclasses import asdict, dataclass
from datetime import date
import gzip
import hashlib
from io import BytesIO
import json
from pathlib import PurePosixPath
import re
import stat
import struct
import tarfile
from types import SimpleNamespace
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

from .domain import stable_hash
from .public_availability import normalized_context, numeric_binding
from .xbrl import MAX_BYTES, UnsafeFinancialDocument, parse_xbrl


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    max_compressed_bytes: int = 64 * 1024 * 1024
    max_decompressed_bytes: int = 512 * 1024 * 1024
    max_member_bytes: int = MAX_BYTES
    max_members: int = 25000
    max_documents: int = 1000
    max_header_bytes: int = 1024 * 1024
    max_emitted_bytes: int = 64 * 1024 * 1024
    max_public_list_bytes: int = 4 * 1024 * 1024

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in asdict(self).values()):
            raise ValueError("archive limits require positive integers")


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    status: str
    reason: str | None
    public_day: date | None
    evidence: dict


class _Gap(ValueError):
    pass


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _identity(document, filing):
    return {"document_id": getattr(document, "id", None), "filing_id": getattr(filing, "id", None),
        "issuer_id": getattr(filing, "issuer_id", None), "source": getattr(filing, "source", None),
        "accession": getattr(filing, "accession", None), "document_hash": getattr(document, "content_hash", None)}


def _unavailable(document, filing, reason):
    return VerificationOutcome("unavailable", reason, None,
        {"binding": _identity(document, filing), "reason": reason})


def _check_identity(document, filing, facts, source):
    if (getattr(filing, "source", None) != source or getattr(document, "filing_id", None) != getattr(filing, "id", None)
            or not getattr(filing, "issuer_id", None) or not getattr(document, "id", None)
            or not re.fullmatch(r"[0-9a-f]{64}", str(getattr(document, "content_hash", "")))):
        raise _Gap("document_binding_mismatch")
    if any(getattr(row, "issuer_id", filing.issuer_id) != filing.issuer_id for row in facts):
        raise _Gap("sql_inventory:issuer_mismatch")
    try:
        return numeric_binding(document, facts)
    except ValueError as exc:
        # The core binding emits fixed reason text, never source numeric bytes.
        raise _Gap("sql_inventory:" + str(exc).removeprefix("numeric_binding: ").replace(" ", "_")) from exc


def _trusted_url(url, *, hosts, path):
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in hosts and parsed.port in {None, 443}
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
            and re.fullmatch(path, parsed.path) is not None)
    except ValueError:
        return False


def _safe_name(name):
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (not name or "\x00" in name or path.is_absolute() or ".." in path.parts
            or ":" in name or any(ord(character) < 32 for character in name)):
        raise _Gap("unsafe_archive:entry_path")
    return normalized


class _BoundedGzip:
    """Read no more than the expansion budget, including skipped TAR members."""
    def __init__(self, stream, limit):
        self.stream, self.limit, self.count = stream, limit, 0

    def read(self, size):
        body = self.stream.read(min(size, self.limit - self.count + 1))
        self.count += len(body)
        if self.count > self.limit:
            raise _Gap("unsafe_archive:decompressed_size_limit")
        return body

    def exact(self, size):
        parts, left = [], size
        while left:
            part = self.read(min(left, 64 * 1024))
            if not part:
                raise _Gap("unsafe_archive:truncated_tar")
            parts.append(part)
            left -= len(part)
        return b"".join(parts)


    def drain(self, size):
        """Consume an unrelated body without allocating its declared member size."""
        left = size
        while left:
            part = self.read(min(left, 64 * 1024))
            if not part:
                raise _Gap("unsafe_archive:truncated_tar")
            left -= len(part)


def _sec_prefix_matches(prefix, accession, limits):
    first_document = re.search(rb"(?im)^\s*<DOCUMENT>\s*$", prefix)
    header = prefix[:first_document.start()] if first_document else prefix
    if (first_document is None and len(prefix) > limits.max_header_bytes
            and (b"<SEC-HEADER>" in header.upper() or b"<SUBMISSION>" in header.upper())):
        raise _Gap("unsafe_archive:submission_header_limit")
    values = re.findall(rb"(?im)^\s*ACCESSION NUMBER\s*:\s*([^\r\n]+)", header)
    values += re.findall(rb"(?im)^\s*<ACCESSION-NUMBER>\s*([^<\r\n]+)", header)
    return any(value.strip() == accession.encode("ascii") for value in values)


def _sec_tar_members(data, limits, *, accession, form, expected_cik, archive_day):
    """Read standard TAR headers before any allocation of a member body.

    PAX/GNU extensions and links are rejected rather than delegating their
    unbounded metadata parsing to tarfile. Regulator accession names fit USTAR.
    Every trailing member and compressed trailer is checked before qualification.
    A bounded SGML prefix supplies actual accession membership. Only the target
    is retained under the member limit; unrelated bodies are stream-drained.
    """
    if not isinstance(data, bytes) or len(data) > limits.max_compressed_bytes:
        raise _Gap("unsafe_archive:compressed_size_limit")
    if not data.startswith(b"\x1f\x8b"):
        raise _Gap("unsafe_archive:invalid_gzip")
    names, count = set(), 0
    try:
        with gzip.GzipFile(fileobj=BytesIO(data), mode="rb") as stream:
            reader = _BoundedGzip(stream, limits.max_decompressed_bytes)
            while True:
                header = reader.exact(512)
                if header == b"\0" * 512:
                    if reader.exact(512) != b"\0" * 512:
                        raise _Gap("unsafe_archive:invalid_tar_terminator")
                    while tail := reader.read(64 * 1024):
                        if tail.strip(b"\0"):
                            raise _Gap("unsafe_archive:trailing_tar_data")
                    return
                try:
                    member = tarfile.TarInfo.frombuf(header, encoding="utf-8", errors="strict")
                except (tarfile.HeaderError, UnicodeError, ValueError) as exc:
                    raise _Gap("unsafe_archive:invalid_tar_header") from exc
                count += 1
                if count > limits.max_members:
                    raise _Gap("unsafe_archive:member_count_limit")
                name = _safe_name(member.name)
                if name in names:
                    raise _Gap("unsafe_archive:duplicate_member")
                names.add(name)
                if member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE}:
                    raise _Gap("unsafe_archive:nonregular_member")
                if member.size < 0:
                    raise _Gap("unsafe_archive:member_size_limit")
                if member.isdir() and member.size:
                    raise _Gap("unsafe_archive:directory_body")
                matched, body = False, None
                if member.isfile():
                    # The extra 64 bytes let a DOCUMENT marker beginning at the
                    # header budget boundary be recognized without growing it.
                    prefix = reader.exact(min(member.size, limits.max_header_bytes + 64))
                    matched = _sec_prefix_matches(prefix, accession, limits)
                    remaining = member.size - len(prefix)
                    if matched:
                        _sec_header(prefix, accession, form, expected_cik, archive_day, limits,
                            require_complete=False)
                        if member.size > limits.max_member_bytes:
                            raise _Gap("unsafe_archive:member_size_limit")
                        body = prefix + reader.exact(remaining)
                    else:
                        reader.drain(remaining)
                padding = (-member.size) % 512
                if padding:
                    reader.drain(padding)
                if matched:
                    yield name, body
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        raise _Gap("unsafe_archive:invalid_gzip") from exc


def _one_header_value(header, *, label, tag):
    text = re.findall(rb"(?im)^\s*" + label + rb"\s*:\s*([^\r\n]+)", header)
    sgml = re.findall(rb"(?im)^\s*<" + tag + rb">\s*([^<\r\n]+)", header)
    values = [item.strip().decode("ascii", errors="strict") for item in text + sgml]
    if len(values) != 1:
        raise _Gap("sec_header:missing_or_ambiguous_field")
    return values[0]


def _sec_header(body, accession, form, expected_cik, archive_day, limits, *, require_complete=True):
    first_document = re.search(rb"(?im)^\s*<DOCUMENT>\s*$", body)
    if not first_document or first_document.start() > limits.max_header_bytes:
        raise _Gap("sec_header:complete_submission_missing")
    header = body[:first_document.start()]
    if not (b"<SEC-HEADER>" in header.upper() and b"</SEC-HEADER>" in header.upper()
            or b"<SUBMISSION>" in header.upper()):
        raise _Gap("sec_header:missing_submission_header")
    if require_complete and not re.search(rb"(?im)^\s*</(?:SEC-DOCUMENT|SUBMISSION)>\s*$", body[first_document.start():]):
        raise _Gap("sec_header:incomplete_submission")
    if re.search(rb"(?im)^\s*<(?:DELETION|CORRECTION|PRIVATE-TO-PUBLIC)>", header):
        raise _Gap("sec_header:unsupported_dissemination_event")
    try:
        found_accession = _one_header_value(header, label=b"ACCESSION NUMBER", tag=b"ACCESSION-NUMBER")
        found_form = _one_header_value(header, label=b"CONFORMED SUBMISSION TYPE", tag=b"TYPE")
        filed = _one_header_value(header, label=b"FILED AS OF DATE", tag=b"FILING-DATE")
        ciks = re.findall(rb"(?im)^\s*(?:CENTRAL INDEX KEY\s*:\s*|<CIK>\s*)([0-9]+)\s*$", header)
        if found_accession != accession or found_form != form:
            raise _Gap("sec_header:accession_or_form_mismatch")
        if not ciks or any(int(cik) != int(expected_cik) for cik in ciks):
            raise _Gap("sec_header:issuer_cik_mismatch")
        if not re.fullmatch(r"[0-9]{8}", filed):
            raise _Gap("sec_header:invalid_filed_date")
        filed_day = date(int(filed[:4]), int(filed[4:6]), int(filed[6:]))
        if filed_day > archive_day:
            raise _Gap("sec_header:future_filed_date")
        return {"accession": found_accession, "form": found_form, "ciks": sorted({str(int(cik)) for cik in ciks}),
            "filed_as_of_date": filed_day.isoformat(), "later_dissemination": filed_day < archive_day}
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, _Gap):
            raise
        raise _Gap("sec_header:invalid_identity") from exc


def _sec_documents(body, limits):
    documents = re.findall(rb"(?ims)^\s*<DOCUMENT>\s*\r?\n(.*?)^\s*</DOCUMENT>\s*$", body)
    if not documents or len(documents) > limits.max_documents:
        raise _Gap("sec_original:document_count_limit")
    if len(documents) != len(re.findall(rb"(?im)^\s*<DOCUMENT>\s*$", body)):
        raise _Gap("sec_original:incomplete_document_members")
    names = set()
    for member in documents:
        start = re.search(rb"(?im)^\s*<TEXT>[^\S\r\n]*\r?\n", member)
        end = re.search(rb"(?im)^\s*</TEXT>[^\S\r\n]*$", member)
        if not start or not end or end.start() < start.end():
            raise _Gap("sec_original:incomplete_document_text")
        metadata = member[:start.start()]
        try:
            name = _one_header_value(metadata, label=b"FILENAME", tag=b"FILENAME")
            _one_header_value(metadata, label=b"TYPE", tag=b"TYPE")
        except _Gap as exc:
            raise _Gap("sec_original:missing_document_identity") from exc
        name = _safe_name(name)
        if name in names:
            raise _Gap("sec_original:ambiguous_document_member")
        names.add(name)
        original = member[start.end():end.start()]
        # The newline delimiting </TEXT> is SGML framing, not the source body.
        if original.endswith(b"\r\n"):
            original = original[:-2]
        elif original.endswith(b"\n"):
            original = original[:-1]
        yield name, original


def _parsed_document(result, identifier):
    return SimpleNamespace(id=identifier, extraction_complete=result.complete,
        inventory={"counts": asdict(result.inventory), "contexts": {key: asdict(ctx) for key, ctx in result.contexts.items()},
            "units": result.units, "issues": [asdict(issue) for issue in result.issues],
            "source_format_complete": result.complete})


def _original_binding(result, document):
    try:
        return numeric_binding(_parsed_document(result, document.id), result.facts)
    except ValueError as exc:
        raise _Gap("original_inventory:" + str(exc).removeprefix("numeric_binding: ").replace(" ", "_")) from exc


def _context_mapping(document, result):
    original = {}
    for key, context in result.contexts.items():
        original.setdefault(stable_hash(normalized_context(asdict(context))), []).append(key)
    mapping = {}
    for key, context in document.inventory["contexts"].items():
        matches = original.get(stable_hash(normalized_context(context)))
        if not matches:
            raise _Gap("original_inventory:context_mapping_missing")
        mapping[key] = sorted(matches)
    return mapping


def _parse_candidates(entries, document, sql_binding, limits, *, expected_cik=None, expected_corp_code=None):
    matches, budget = [], limits.max_emitted_bytes
    supported = False
    failure = "original_inventory:full_numeric_semantics_mismatch"
    for name, body, provenance in entries:
        if PurePosixPath(name).suffix.lower() not in {".xml", ".xbrl", ".xhtml", ".htm", ".html"}:
            continue
        try:
            result = parse_xbrl(body, max_bytes=limits.max_member_bytes, max_emitted_bytes=budget)
        except UnsafeFinancialDocument:
            raise _Gap("original_inventory:unsafe_numeric_document") from None
        except ValueError:
            continue
        budget -= result.inventory.emitted_bytes
        if not result.inventory.numeric_nodes:
            continue
        supported = True
        if expected_cik is not None and any(not context.entity_scheme or
                context.entity_scheme.rstrip("/") not in {"http://www.sec.gov/CIK", "https://www.sec.gov/CIK"}
                or not context.entity_identifier or not context.entity_identifier.isdigit()
                or int(context.entity_identifier) != int(expected_cik) for context in result.contexts.values()):
            failure = "original_inventory:issuer_context_mismatch"
            continue
        if expected_corp_code is not None and any(context.entity_scheme != "opendart.corp_code"
                or context.entity_identifier != expected_corp_code for context in result.contexts.values()):
            failure = "original_inventory:issuer_context_mismatch"
            continue
        try:
            original_binding = _original_binding(result, document)
        except _Gap as exc:
            failure = str(exc)
            continue
        if original_binding != sql_binding:
            continue
        matches.append({"name": name, "hash": _sha(body), "binding": original_binding,
            "context_mapping": _context_mapping(document, result), "provenance": provenance})
    if not matches:
        raise _Gap(failure if supported else "original_inventory:supported_numeric_instance_unavailable")
    exact = [match for match in matches if match["hash"] == document.content_hash]
    chosen = exact or matches
    if len(chosen) != 1:
        raise _Gap("original_inventory:ambiguous_original_member")
    match = chosen[0]
    match["version_match"] = "exact_original_body" if exact else "complete_semantic_inventory"
    return match


def verify_sec_dated_feed(*, document, filing, sql_facts, archive_bytes, archive_url,
        archive_day, expected_cik, limits=None) -> VerificationOutcome:
    """Verify a complete filing in an official Feed/Oldloads DAY archive.

    Header filing/acceptance dates never replace the archive dissemination day.
    Generated instances require every numeric node and structural context to
    match an actual disseminated original, including all duplicate nodes.
    """
    limits = limits or ArchiveLimits()
    facts = list(sql_facts)
    try:
        if type(archive_day) is not date or not re.fullmatch(r"[0-9]{1,10}", str(expected_cik)):
            raise _Gap("sec_archive:invalid_day_or_cik")
        if not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", str(getattr(filing, "accession", ""))):
            raise _Gap("sec_archive:invalid_accession")
        if not _trusted_url(archive_url, hosts={"www.sec.gov", "sec.gov"},
                path=r"/Archives/edgar/(?:Feed|Oldloads)/[0-9]{4}/QTR[1-4]/[0-9]{8}\.nc\.tar\.gz"):
            raise _Gap("sec_archive:untrusted_distribution_url")
        quarter = (archive_day.month - 1) // 3 + 1
        if urlsplit(archive_url).path.rsplit("/", 3)[1:] != [str(archive_day.year), f"QTR{quarter}", archive_day.strftime("%Y%m%d") + ".nc.tar.gz"]:
            raise _Gap("sec_archive:distribution_day_mismatch")
        sql_binding = _check_identity(document, filing, facts, "sec")
        entries, matched_submissions = [], []
        for name, body in _sec_tar_members(archive_bytes, limits, accession=filing.accession,
                form=filing.form, expected_cik=expected_cik, archive_day=archive_day):
            header = _sec_header(body, filing.accession, filing.form, expected_cik, archive_day, limits)
            matched_submissions.append(name)
            if len(matched_submissions) != 1:
                raise _Gap("sec_archive:ambiguous_submission_membership")
            for original_name, original in _sec_documents(body, limits):
                entries.append((original_name, original,
                    {"submission_member_name": name, "submission_member_hash": _sha(body), "header": header}))
        if not matched_submissions:
            raise _Gap("sec_archive:accession_not_in_disseminated_bundle")
        original = _parse_candidates(entries, document, sql_binding, limits, expected_cik=expected_cik)
        provenance = original["provenance"]
        proof = {"kind": "sec_dated_feed_original", "public_day": archive_day.isoformat(),
            "issuer_id": filing.issuer_id, "accession": filing.accession, "source_url": archive_url,
            "source_body_hash": _sha(archive_bytes), "original_body_hash": original["hash"],
            "membership_verified": True, "original_version_verified": True, "member_hash": original["hash"],
            "member_name": provenance["submission_member_name"] + "::" + original["name"], "archive_url": archive_url,
            "original_document_name": original["name"], "submission_member_hash": provenance["submission_member_hash"],
            "header": provenance["header"], "later_dissemination": provenance["header"]["later_dissemination"],
            "version_match": original["version_match"]}
        return VerificationOutcome("qualified", None, archive_day,
            {"binding": _identity(document, filing), "proof": proof,
             "numeric_binding": {**sql_binding, "original_digest": original["binding"]["sql_digest"]},
             "context_mapping": original["context_mapping"]})
    except _Gap as exc:
        return _unavailable(document, filing, str(exc))


def _dart_zip_members(data, limits):
    if not isinstance(data, bytes) or len(data) > limits.max_compressed_bytes:
        raise _Gap("unsafe_archive:compressed_size_limit")
    _zip_directory_bounds(data, limits)
    names, expanded = set(), 0
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > limits.max_members:
                raise _Gap("unsafe_archive:member_count_limit")
            for entry in entries:
                name = _safe_name(entry.filename)
                if name in names:
                    raise _Gap("unsafe_archive:duplicate_member")
                names.add(name)
                mode = entry.external_attr >> 16
                if (stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
                        or entry.flag_bits & 1):
                    raise _Gap("unsafe_archive:nonregular_or_encrypted_member")
                if entry.file_size > limits.max_member_bytes:
                    raise _Gap("unsafe_archive:member_size_limit")
                expanded += entry.file_size
                if expanded > limits.max_decompressed_bytes:
                    raise _Gap("unsafe_archive:decompressed_size_limit")
                if entry.is_dir():
                    if entry.file_size:
                        raise _Gap("unsafe_archive:directory_body")
                    continue
                with archive.open(entry) as stream:
                    pieces, actual = [], 0
                    while piece := stream.read(min(64 * 1024, limits.max_member_bytes - actual + 1)):
                        actual += len(piece)
                        if actual > limits.max_member_bytes or actual > entry.file_size:
                            raise _Gap("unsafe_archive:member_size_limit")
                        pieces.append(piece)
                    if actual != entry.file_size:
                        raise _Gap("unsafe_archive:member_size_mismatch")
                yield name, b"".join(pieces)
    except (BadZipFile, OSError, EOFError, NotImplementedError, RuntimeError) as exc:
        raise _Gap("unsafe_archive:invalid_zip") from exc


def _zip_directory_bounds(data, limits):
    """Bound entry allocation before ZipFile builds its entire central inventory."""
    end = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    if end < 0 or end + 22 > len(data):
        raise _Gap("unsafe_archive:invalid_zip")
    disk, central_disk, disk_count, total_count, size, offset, comment = struct.unpack_from("<4H2IH", data, end + 4)
    if total_count > limits.max_members or disk_count > limits.max_members:
        raise _Gap("unsafe_archive:member_count_limit")
    if (disk or central_disk or disk_count != total_count or total_count == 65535
            or size == 0xffffffff or offset == 0xffffffff):
        raise _Gap("unsafe_archive:unsupported_zip_directory")
    if end + 22 + comment != len(data) or offset + size != end or offset > end:
        raise _Gap("unsafe_archive:invalid_zip_directory")
    cursor, actual, expanded = offset, 0, 0
    while cursor < end:
        if cursor + 46 > end or data[cursor:cursor + 4] != b"PK\x01\x02":
            raise _Gap("unsafe_archive:invalid_zip_directory")
        actual += 1
        if actual > limits.max_members:
            raise _Gap("unsafe_archive:member_count_limit")
        member_size = struct.unpack_from("<I", data, cursor + 24)[0]
        if member_size > limits.max_member_bytes:
            raise _Gap("unsafe_archive:member_size_limit")
        expanded += member_size
        if expanded > limits.max_decompressed_bytes:
            raise _Gap("unsafe_archive:decompressed_size_limit")
        name_size, extra_size, comment_size = struct.unpack_from("<3H", data, cursor + 28)
        cursor += 46 + name_size + extra_size + comment_size
        if cursor > end:
            raise _Gap("unsafe_archive:invalid_zip_directory")
    if actual != total_count:
        raise _Gap("unsafe_archive:zip_entry_count_mismatch")


def _dart_list(body, filing, expected_corp_code, requested_receipt, limits):
    if not isinstance(body, bytes) or len(body) > limits.max_public_list_bytes:
        raise _Gap("dart_public_list:body_limit")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _Gap("dart_public_list:invalid_json") from exc
    if not isinstance(payload, dict) or payload.get("status") != "000" or not isinstance(payload.get("list"), list):
        raise _Gap("dart_public_list:unavailable")
    items = [item for item in payload["list"] if isinstance(item, dict) and item.get("rcept_no") == requested_receipt]
    if len(items) != 1:
        raise _Gap("dart_public_list:receipt_missing_or_ambiguous")
    item = items[0]
    if item.get("corp_code") != expected_corp_code or item.get("report_nm") != filing.form:
        raise _Gap("dart_public_list:company_or_title_mismatch")
    day = item.get("rcept_dt", "")
    if not isinstance(day, str) or not re.fullmatch(r"[0-9]{8}", day):
        raise _Gap("dart_public_list:invalid_receipt_day")
    if day != requested_receipt[:8]:
        raise _Gap("dart_public_list:receipt_day_mismatch")
    try:
        public_day = date(int(day[:4]), int(day[4:6]), int(day[6:]))
    except ValueError as exc:
        raise _Gap("dart_public_list:invalid_receipt_day") from exc
    return public_day, {key: item[key] for key in ("corp_code", "rcept_no", "rcept_dt", "report_nm")}


def verify_dart_receipt_original(*, document, filing, sql_facts, archive_bytes, original_url,
        public_list_body, public_list_url, expected_corp_code, requested_receipt, limits=None) -> VerificationOutcome:
    """Verify receipt-specific XBRL ZIP plus exact official public-list membership.

    requested_receipt is the actual fnlttXbrl request parameter, supplied by the
    trusted transport caller. Latest JSON and viewer responses cannot qualify.
    Unmapped JSON period/scope identities remain unavailable under full binding.
    """
    limits = limits or ArchiveLimits()
    facts = list(sql_facts)
    try:
        if (not re.fullmatch(r"[0-9]{8}", str(expected_corp_code))
                or not re.fullmatch(r"[0-9]{14}", str(requested_receipt))
                or requested_receipt != getattr(filing, "accession", None)):
            raise _Gap("dart_original:receipt_or_company_mismatch")
        if not _trusted_url(original_url, hosts={"opendart.fss.or.kr"}, path=r"/api/fnlttXbrl\.xml"):
            raise _Gap("dart_original:receipt_specific_endpoint_required")
        if not _trusted_url(public_list_url, hosts={"opendart.fss.or.kr"}, path=r"/api/list\.json"):
            raise _Gap("dart_public_list:untrusted_source_url")
        public_day, list_item = _dart_list(public_list_body, filing, expected_corp_code, requested_receipt, limits)
        sql_binding = _check_identity(document, filing, facts, "opendart")
        source_request = document.inventory.get("source_request_metadata", {})
        if source_request.get("rcept_no") not in {None, requested_receipt}:
            raise _Gap("dart_original:stored_request_receipt_mismatch")
        entries = [(name, body, {}) for name, body in _dart_zip_members(archive_bytes, limits)]
        if len(entries) > limits.max_documents:
            raise _Gap("dart_original:document_count_limit")
        original = _parse_candidates(entries, document, sql_binding, limits, expected_corp_code=expected_corp_code)
        # SQL XBRL documents bind the full ZIP hash, while SEC documents bind a
        # single body. The full semantic match remains mandatory in both cases.
        version_match = "exact_original_archive" if _sha(archive_bytes) == document.content_hash else original["version_match"]
        proof = {"kind": "dart_receipt_original", "public_day": public_day.isoformat(),
            "issuer_id": filing.issuer_id, "accession": requested_receipt, "source_url": public_list_url,
            "source_body_hash": _sha(public_list_body), "original_body_hash": original["hash"],
            "membership_verified": True, "original_version_verified": True, "receipt_specific_original": True,
            "original_url": original_url, "original_archive_hash": _sha(archive_bytes),
            "original_document_name": original["name"], "public_list_item": list_item,
            "request_parameters": {"rcept_no": requested_receipt}, "version_match": version_match}
        return VerificationOutcome("qualified", None, public_day,
            {"binding": _identity(document, filing), "proof": proof,
             "numeric_binding": {**sql_binding, "original_digest": original["binding"]["sql_digest"]},
             "context_mapping": original["context_mapping"]})
    except _Gap as exc:
        return _unavailable(document, filing, str(exc))
