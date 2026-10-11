"""Pure, frozen-membership public clocks and an idempotent certificate append gate.

The certificate qualifies information availability only. Extraction adoption,
financial semantics and trading-session eligibility remain separate gates.
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re
from types import SimpleNamespace
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ..db import new_id, utcnow
from .publication_models import FinancialPublicationEvidence


POLICY_VERSION = "financial_public_pit_v1"
SOURCE_TIMEZONES = {"sec": "America/New_York", "opendart": "Asia/Seoul"}
PROOF_KINDS = {"sec": "sec_dated_feed_original", "opendart": "dart_receipt_original"}
STATUSES = {"qualified", "unavailable", "rejected", "withdrawal"}
XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"


def _aware(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} requires a timezone-aware timestamp")
    return value


def conservative_public_bound(public_day: date, timezone_name: str) -> datetime:
    """Following local calendar midnight, including 23/25-hour DST days."""
    if type(public_day) is not date:
        raise ValueError("public_day requires an exact calendar date")
    if timezone_name not in SOURCE_TIMEZONES.values():
        raise ValueError("unsupported public timezone")
    try:
        next_day = public_day + timedelta(days=1)
        return datetime.combine(next_day, time.min, tzinfo=ZoneInfo(timezone_name)).astimezone(timezone.utc)
    except OverflowError as exc:
        raise ValueError("public_day has no representable following local-day bound") from exc


def _json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _hash(value):
    return hashlib.sha256(json.dumps(_json_value(value), sort_keys=True,
        separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _identity(document, filing):
    return {"document_id": document.id, "filing_id": filing.id, "issuer_id": filing.issuer_id,
        "source": filing.source, "accession": filing.accession, "document_hash": document.content_hash}


def _canonical_xml(element):
    return [element.tag, sorted(element.attrib.items()), (element.text or "").strip(),
        [_canonical_xml(child) for child in element]]


def _dimensions(dimensions):
    result = {}
    for key, value in dimensions.items():
        if not isinstance(key, str) or not key.startswith("{") or not isinstance(value, str):
            raise ValueError("numeric_binding: unresolved dimensions")
        if value.startswith("<"):
            try:
                wrapped = ET.fromstring("<root>" + value + "</root>")
                result[key] = [_canonical_xml(child) for child in wrapped]
            except ET.ParseError as exc:
                raise ValueError("numeric_binding: unsupported typed dimension") from exc
        elif value.startswith("{"):
            result[key] = value
        else:
            raise ValueError("numeric_binding: unresolved dimension member")
    return result


def normalized_context(context):
    """Canonical full context structure for original-to-SQL context mappings."""
    if context.get("reported", True) is False or not context.get("entity_identifier") or not context.get("entity_scheme"):
        raise ValueError("numeric_binding: incomplete entity context")
    kind = context.get("period_kind")
    if kind not in {"instant", "duration"} or not context.get("period_end"):
        raise ValueError("numeric_binding: unsupported period context")
    if kind == "duration" and not context.get("period_start"):
        raise ValueError("numeric_binding: incomplete duration context")
    raw_xml = context.get("raw_xml")
    if not raw_xml:
        raise ValueError("numeric_binding: missing original context structure")
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as exc:
        raise ValueError("numeric_binding: invalid context structure") from exc
    if root.tag != f"{{{XBRLI}}}context":
        raise ValueError("numeric_binding: unsupported context structure")
    allowed_children = {"context": {"entity", "period", "scenario"},
        "entity": {"identifier", "segment"}, "period": {"instant", "startDate", "endDate"},
        "identifier": set(), "instant": set(), "startDate": set(), "endDate": set()}
    pending = [root]
    while pending:
        node = pending.pop()
        if node.tag in {f"{{{XBRLI}}}segment", f"{{{XBRLI}}}scenario"}:
            if any(child.tag not in {f"{{{XBRLDI}}}explicitMember", f"{{{XBRLDI}}}typedMember"} for child in node):
                raise ValueError("numeric_binding: unsupported non-dimensional context")
            continue
        local = node.tag.removeprefix(f"{{{XBRLI}}}")
        allowed = {f"{{{XBRLI}}}{name}" for name in allowed_children.get(local, set())}
        if any(child.tag not in allowed for child in node) or len({child.tag for child in node}) != len(node):
            raise ValueError("numeric_binding: unsupported context structure")
        pending.extend(node)
    return _json_value({"entity_identifier": context["entity_identifier"], "entity_scheme": context["entity_scheme"],
        "period_kind": kind, "period_start": context.get("period_start"), "period_end": context["period_end"],
        "dimensions": _dimensions(context.get("dimensions", {})), "scope": context.get("scope", "unknown")})


def _normal_value(value):
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError("numeric_binding: non-finite number")
    sign, digits, exponent = number.as_tuple()
    digits = list(digits)
    if not any(digits):
        return "0"
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    return ("-" if sign else "") + "".join(str(digit) for digit in digits) + "e" + str(exponent)


def numeric_binding(document, source_facts) -> dict:
    """Full structural/numeric digest; opaque node/context/prefix labels are omitted.

    Both stored SQL facts and parser SourceFact objects are accepted. Each fact's
    source-node multiplicity is preserved, and every SQL inventory counter is
    checked independently before any equivalence is asserted.
    """
    rows = list(source_facts)
    inventory = document.inventory
    counts = inventory.get("counts", {})
    if (not document.extraction_complete or not inventory.get("source_format_complete", False)
            or inventory.get("issues") or counts.get("unsupported_nodes", 0) or counts.get("rejected_nodes", 0)):
        raise ValueError("numeric_binding: incomplete source extraction")
    contexts = inventory.get("contexts", {})
    if set(counts.get("context_ids", [])) != set(contexts):
        raise ValueError("numeric_binding: context inventory mismatch")
    if set(counts.get("unit_ids", [])) != set(inventory.get("units", {})):
        raise ValueError("numeric_binding: unit inventory mismatch")
    normalized_contexts = {key: normalized_context(value) for key, value in contexts.items()}
    nodes, normalized, identities = [], [], {}
    for row in rows:
        evidence = row.evidence
        source_nodes = evidence.get("source_node_ids", getattr(row, "source_node_ids", None))
        if not isinstance(source_nodes, (list, tuple)) or not source_nodes or any(not isinstance(node, str) or not node for node in source_nodes):
            raise ValueError("numeric_binding: missing source nodes")
        nodes.extend(source_nodes)
        if getattr(row, "document_id", document.id) != document.id or row.context_id not in normalized_contexts:
            raise ValueError("numeric_binding: wrong document or missing context")
        context = normalized_contexts[row.context_id]
        concept = evidence.get("concept_qname")
        if not isinstance(concept, str) or not concept.startswith("{"):
            raise ValueError("numeric_binding: unresolved concept identity")
        unit_bindings = evidence.get("unit_measure_bindings")
        if not isinstance(unit_bindings, list) or not unit_bindings:
            raise ValueError("numeric_binding: missing expanded units")
        units = []
        for unit in unit_bindings:
            if unit.get("role") not in {"measure", "numerator", "denominator"} or not str(unit.get("measure_qname", "")).startswith("{"):
                raise ValueError("numeric_binding: unresolved unit identity")
            units.append((unit["role"], unit["measure_qname"]))
        def rendered_unit(role, *, lexical=False):
            values = []
            for unit in unit_bindings:
                if unit["role"] != role:
                    continue
                measure = unit.get("measure_lexical") if lexical else unit["measure_qname"]
                if not isinstance(measure, str) or not measure:
                    raise ValueError("numeric_binding: unresolved original unit")
                if not lexical and measure.split("}", 1)[0][1:] in {XBRLI, "http://www.xbrl.org/2003/iso4217"}:
                    measure = measure.split("}", 1)[1]
                values.append(measure)
            return "*".join(values if lexical else sorted(values))
        roles = {item[0] for item in units}
        if roles == {"measure"}:
            expected_unit, expected_original = rendered_unit("measure"), rendered_unit("measure", lexical=True)
        elif roles == {"numerator", "denominator"}:
            expected_unit = rendered_unit("numerator") + "/" + rendered_unit("denominator")
            expected_original = rendered_unit("numerator", lexical=True) + "/" + rendered_unit("denominator", lexical=True)
        else:
            raise ValueError("numeric_binding: unsupported unit structure")
        if row.unit != expected_unit or row.original_unit != expected_original or row.scope != context["scope"]:
            raise ValueError("numeric_binding: stored unit or scope disagrees with original structure")
        parse_status = evidence.get("parse_status", getattr(row, "parse_status", None))
        if parse_status != "ok":
            raise ValueError("numeric_binding: unsupported numeric node")
        if _dimensions(row.dimensions) != context["dimensions"]:
            raise ValueError("numeric_binding: fact/context dimensions mismatch")
        row_end = getattr(row, "instant", None) if row.period_kind == "instant" else row.period_end
        row_end = row_end or row.period_end
        if (row.period_kind != context["period_kind"] or _json_value(row.period_start) != context["period_start"]
                or _json_value(row_end) != context["period_end"]):
            raise ValueError("numeric_binding: fact/context period mismatch")
        if (row.is_nil and row.value is not None) or (not row.is_nil and row.value is None):
            raise ValueError("numeric_binding: unresolved nil/value")
        item = {"concept": concept, "context": context, "units": sorted(units), "scope": row.scope,
            "is_nil": row.is_nil, "value": None if row.is_nil else _normal_value(row.value),
            "scale": row.scale, "decimals": row.decimals, "precision": evidence.get("precision", getattr(row, "precision", None)),
            "parse_status": parse_status, "sign": evidence.get("sign", getattr(row, "sign", None)),
            "format": evidence.get("format", getattr(row, "format", None))}
        identity = _hash({key: item[key] for key in ("concept", "context", "units", "scope")})
        if identity in identities and identities[identity] != item:
            raise ValueError("numeric_binding: ambiguous equivalent-context facts")
        identities[identity] = item
        normalized.extend([item] * len(source_nodes))
    extracted, numeric = counts.get("extracted_nodes"), counts.get("numeric_nodes")
    deduplicated = counts.get("deduplicated_nodes", 0)
    if (not nodes or len(set(nodes)) != len(nodes) or numeric != len(nodes) or extracted != len(nodes)
            or len(rows) + deduplicated != extracted):
        raise ValueError("numeric_binding: numeric node inventory mismatch")
    payload = {"facts": sorted(normalized, key=_hash),
        "contexts": sorted(normalized_contexts.values(), key=_hash), "unit_count": len(inventory.get("units", {}))}
    return {"sql_digest": _hash(payload), "numeric_nodes": numeric, "extracted_nodes": extracted,
        "deduplicated_nodes": deduplicated, "fact_count": len(rows), "complete": True,
        "unsupported_nodes": 0, "ambiguous_nodes": 0}


def _valid_url(value, source):
    if not isinstance(value, str):
        return False
    hosts = {"sec.gov", "www.sec.gov"} if source == "sec" else {"opendart.fss.or.kr", "dart.fss.or.kr"}
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and parsed.hostname in hosts and parsed.port in {None, 443}
            and not parsed.username and not parsed.password and not parsed.fragment and not parsed.query)
    except ValueError:
        return False


def _sec_distribution_day(url, public_day):
    if not _valid_url(url, "sec"):
        return False
    quarter = (public_day.month - 1) // 3 + 1
    pattern = (rf"/Archives/edgar/(?:Feed|Oldloads)/{public_day.year}/QTR{quarter}/"
        + public_day.strftime("%Y%m%d") + r"\.nc\.tar\.gz")
    return re.fullmatch(pattern, urlsplit(url).path) is not None


def _dart_endpoint(url, path):
    return (_valid_url(url, "opendart") and urlsplit(url).hostname == "opendart.fss.or.kr"
        and urlsplit(url).path == path)


def _certificate_reason(document, filing, row):
    identity = _identity(document, filing)
    if document.filing_id != filing.id or any(getattr(row, key, None) != value for key, value in identity.items()):
        return "publication_identity_mismatch"
    if row.policy_version != POLICY_VERSION:
        return "unsupported_publication_policy"
    if row.status not in STATUSES:
        return "unsupported_publication_status"
    if row.status in {"unavailable", "rejected"}:
        return None
    expected_zone = SOURCE_TIMEZONES.get(filing.source)
    if not expected_zone or row.timezone != expected_zone or row.precision != "public_day":
        return "unsupported_publication_precision"
    if type(row.public_day) is not date:
        return "invalid_public_day"
    try:
        if _aware(row.effective_available_at, "effective_available_at") != conservative_public_bound(row.public_day, expected_zone):
            return "invalid_public_day_bound"
        if row.public_day > _aware(row.captured_at, "captured_at").astimezone(ZoneInfo(expected_zone)).date():
            return "future_public_day"
        if _aware(row.recorded_at, "recorded_at") < row.captured_at:
            return "invalid_evidence_clocks"
    except ValueError:
        return "invalid_evidence_clocks"
    evidence = row.evidence
    if not isinstance(evidence, dict) or evidence.get("binding") != identity:
        return "publication_binding_mismatch"
    proof = evidence.get("proof", {})
    if (not isinstance(proof, dict) or proof.get("kind") != PROOF_KINDS[filing.source]
            or proof.get("issuer_id") != filing.issuer_id or proof.get("accession") != filing.accession
            or proof.get("public_day") != row.public_day.isoformat() or proof.get("membership_verified") is not True
            or proof.get("original_version_verified") is not True or not _valid_url(proof.get("source_url"), filing.source)
            or not _digest(proof.get("source_body_hash")) or not _digest(proof.get("original_body_hash"))):
        return "unverified_authoritative_original"
    if proof.get("version_order_ambiguous"):
        return "ambiguous_publication_assertions"
    if filing.source == "sec" and (proof.get("member_hash") != proof["original_body_hash"]
            or not proof.get("member_name") or not _sec_distribution_day(proof.get("archive_url"), row.public_day)
            or not _sec_distribution_day(proof.get("source_url"), row.public_day)
            or urlsplit(proof["archive_url"]).path != urlsplit(proof["source_url"]).path):
        return "unverified_archive_member"
    if filing.source == "opendart":
        parameters = proof.get("request_parameters")
        if (proof.get("receipt_specific_original") is not True
                or not _dart_endpoint(proof.get("source_url"), "/api/list.json")
                or not _dart_endpoint(proof.get("original_url"), "/api/fnlttXbrl.xml")
                or not isinstance(parameters, dict) or parameters != {"rcept_no": filing.accession}):
            return "unverified_receipt_original"
    if row.status == "withdrawal" and (proof.get("event_kind") != "withdrawal" or proof.get("withdrawal_verified") is not True):
        return "unverified_withdrawal_event"
    binding = evidence.get("numeric_binding", {})
    counts = document.inventory.get("counts", {})
    counter_keys = ("numeric_nodes", "extracted_nodes", "deduplicated_nodes")
    if (not document.extraction_complete or document.inventory.get("issues")
            or not document.inventory.get("source_format_complete", False)
            or counts.get("unsupported_nodes", 0) or counts.get("rejected_nodes", 0)
            or not isinstance(binding, dict) or binding.get("complete") is not True
            or binding.get("unsupported_nodes") != 0 or binding.get("ambiguous_nodes") != 0
            or not _digest(binding.get("sql_digest")) or binding.get("original_digest") != binding.get("sql_digest")):
        return "unverified_full_numeric_binding"
    if (any(type(counts.get(key)) is not int or counts[key] < 0 for key in counter_keys)
            or any(type(binding.get(key)) is not int or binding[key] < 0 for key in (*counter_keys, "fact_count"))
            or any(binding.get(key) != counts[key] for key in counter_keys)
            or binding.get("numeric_nodes", 0) <= 0 or binding["numeric_nodes"] != binding["extracted_nodes"]
            or binding.get("fact_count", -1) + binding["deduplicated_nodes"] != binding["extracted_nodes"]):
        return "numeric_inventory_mismatch"
    return None


def resolve_document_availability(document, filing, evidence_rows, *, history_mode, as_of) -> dict:
    """Resolve only supplied immutable evidence; never consult current SQL state."""
    if history_mode not in {"reconstructed_public", "observed_system"}:
        raise ValueError("unsupported history_mode")
    _aware(as_of, "as_of")
    capture_bound = max(_aware(getattr(row, field), field) for row in (document, filing)
        for field in ("available_at", "captured_at"))
    visible = []
    invalid_reasons = []
    for row in evidence_rows:
        if getattr(row, "document_id", None) != document.id:
            continue
        if history_mode == "observed_system":
            try:
                if max(_aware(row.captured_at, "captured_at"), _aware(row.recorded_at, "recorded_at")) > as_of:
                    continue
            except ValueError:
                continue
        reason = _certificate_reason(document, filing, row)
        if reason:
            invalid_reasons.append(reason)
        else:
            visible.append(row)
    qualified = [row for row in visible if row.status == "qualified"]
    rejected = {row.supersedes_assertion_hash for row in visible if row.status == "rejected"}
    active = [row for row in qualified if row.assertion_hash not in rejected]
    reason = "publication_assertion_rejected" if qualified and not active else (
        "ambiguous_publication_assertions" if "ambiguous_publication_assertions" in invalid_reasons else "capture_only_publication_unverified")
    if not active:
        return {"effective_available_at": capture_bound, "status": "capture_only" if capture_bound <= as_of else "unavailable",
            "reason": reason, "evidence_ids": sorted(row.id for row in visible if row.status == "rejected"),
            "precision": "capture_only", "policy_version": POLICY_VERSION, "withdrawn": False,
            "verified_public_bound": None}
    chosen = min(active, key=lambda row: (row.effective_available_at, row.assertion_hash, row.id))
    bound = capture_bound if history_mode == "observed_system" else chosen.effective_available_at
    targets = {item.assertion_hash: item for item in active}
    withdrawals = [row for row in visible if row.status == "withdrawal"
        and row.supersedes_assertion_hash in targets
        and targets[row.supersedes_assertion_hash].effective_available_at <= row.effective_available_at <= as_of]
    if withdrawals:
        withdrawal = min(withdrawals, key=lambda row: (row.effective_available_at, row.assertion_hash, row.id))
        return {"effective_available_at": bound, "status": "unavailable", "reason": "publication_withdrawn",
            "evidence_ids": sorted({chosen.id, withdrawal.id}), "precision": chosen.precision,
            "policy_version": POLICY_VERSION, "withdrawn": True, "verified_public_bound": chosen.effective_available_at}
    return {"effective_available_at": bound, "status": "qualified" if bound <= as_of else "unavailable",
        "reason": "conservative_public_day_no_verified_session_calendar" if bound <= as_of else "before_public_availability",
        "evidence_ids": [chosen.id], "precision": chosen.precision, "policy_version": POLICY_VERSION, "withdrawn": False,
        "verified_public_bound": chosen.effective_available_at}


def _validate_recording_clocks(document, filing, source_facts, *, captured_at, recorded_at):
    clocks = [_aware(captured_at, "captured_at"), _aware(document.captured_at, "document.captured_at"),
        _aware(filing.captured_at, "filing.captured_at")]
    for row in source_facts:
        clocks.extend(_aware(getattr(row, field), field) for field in ("captured_at", "first_seen_at", "extracted_at"))
    if _aware(recorded_at, "recorded_at") < max(clocks):
        raise ValueError("publication_recording_before_bound_observation")


def append_publication_evidence(session, *, document, filing, status, evidence, public_day=None,
        captured_at, recorded_at=None, supersedes_assertion_hash=None) -> FinancialPublicationEvidence:
    """Append one assertion; exact replays return the first row and its real clocks."""
    from .models import SourceFinancialFact

    if status not in STATUSES:
        raise ValueError("unsupported publication status")
    _aware(captured_at, "captured_at")
    recorded_at = _aware(recorded_at or utcnow(), "recorded_at")
    if recorded_at < captured_at:
        raise ValueError("recorded_at cannot precede captured_at")
    if document.filing_id != filing.id:
        raise ValueError("publication_identity_mismatch")
    identity = _identity(document, filing)
    evidence = _json_value(evidence)
    if not isinstance(evidence, dict):
        raise ValueError("publication evidence must be a JSON object")
    if status in {"unavailable", "rejected"}:
        if "binding" in evidence and evidence["binding"] != identity:
            raise ValueError("publication_binding_mismatch")
        evidence = {**evidence, "binding": identity}
    zone = SOURCE_TIMEZONES.get(filing.source) if status in {"qualified", "withdrawal"} else None
    bound = conservative_public_bound(public_day, zone) if zone else None
    values = {**identity, "status": status, "public_day": public_day, "effective_available_at": bound,
        "precision": "public_day" if bound else "capture_only", "timezone": zone,
        "policy_version": POLICY_VERSION, "captured_at": captured_at, "recorded_at": recorded_at,
        "supersedes_assertion_hash": supersedes_assertion_hash, "evidence": evidence}
    row = SimpleNamespace(**values)
    reason = _certificate_reason(document, filing, row)
    if reason:
        raise ValueError(reason)
    if status in {"rejected", "withdrawal"} and not supersedes_assertion_hash:
        raise ValueError("publication event requires supersedes_assertion_hash")
    if supersedes_assertion_hash:
        target = session.scalar(select(FinancialPublicationEvidence).where(
            FinancialPublicationEvidence.assertion_hash == supersedes_assertion_hash))
        if target is None or target.status != "qualified" or any(getattr(target, key) != value for key, value in identity.items()):
            raise ValueError("publication_supersession_target_mismatch")
        if recorded_at < target.recorded_at:
            raise ValueError("publication_event_recording_before_target")
        if status == "withdrawal" and bound < target.effective_available_at:
            raise ValueError("withdrawal_before_original_publication")
    if status in {"qualified", "withdrawal"}:
        rows = session.scalars(select(SourceFinancialFact).where(SourceFinancialFact.document_id == document.id)).all()
        _validate_recording_clocks(document, filing, rows, captured_at=captured_at, recorded_at=recorded_at)
        sql_binding = numeric_binding(document, rows)
        if any(evidence["numeric_binding"].get(key) != value for key, value in sql_binding.items()):
            raise ValueError("publication_sql_numeric_binding_mismatch")
    assertion_hash = _hash({key: value for key, value in values.items() if key not in {"captured_at", "recorded_at"}})
    session.execute(insert(FinancialPublicationEvidence).values(id=new_id(), assertion_hash=assertion_hash, **values)
        .on_conflict_do_nothing(index_elements=[FinancialPublicationEvidence.assertion_hash]))
    return session.scalar(select(FinancialPublicationEvidence).where(
        FinancialPublicationEvidence.assertion_hash == assertion_hash))
