"""Pragmatic daily public PIT bound to exact regulator metadata/version refs.

No original archive, body hash, or deep numeric-binding certificate is required
by this policy. It never changes strict financial_public_pit_v1 certificates,
source observations, capture clocks, or accounting periods.
"""
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from ..db import new_id, utcnow
from .domain import stable_hash
from .selection import AmbiguousFinancialRevision, fact_identity, fact_visible
from .storage import json_value
from .daily_publication_models import FinancialDailyPublicationEvidence


POLICY_VERSION = "financial_daily_metadata_pit_v1"
QUERY_VERSION = "release_query_v8_daily"
MARKETS = {"US": ("America/New_York", time(9, 30)), "KR": ("Asia/Seoul", time(9))}


def _aware(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name}: aware timestamp required")
    return value


def _day(value):
    if type(value) is date:
        return value
    if not isinstance(value, str):
        raise ValueError("source publication date is invalid")
    if re.fullmatch(r"\d{8}", value):
        value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("source publication date must retain day precision")
    return date.fromisoformat(value)


def source_version_ref(filing, source_fact=None):
    evidence = getattr(source_fact, "evidence", {}) or {}
    row = evidence.get("source_row", {})
    if not isinstance(row, Mapping):
        row = {}
    # Returned payload identity wins over the originally requested report.
    if filing.source == "opendart":
        version = row.get("rcept_no") or evidence.get("returned_rcept_no") or evidence.get("receipt") or filing.accession
        valid = re.fullmatch(r"\d{14}", str(version))
    elif filing.source == "sec":
        version = row.get("accn") or row.get("accessionNumber") or evidence.get("accn") or filing.accession
        valid = re.fullmatch(r"\d{10}-\d{2}-\d{6}", str(version))
    else:
        raise ValueError("unsupported_daily_publication_source")
    if not valid:
        raise ValueError("invalid_source_version_reference")
    return str(version)


def next_market_session(public_day, market, sessions=()):
    if market not in MARKETS:
        raise ValueError("issuer market must be US or KR")
    zone = ZoneInfo(MARKETS[market][0])
    valid = [s for s in sessions if s.market == market and s.session > public_day]
    if valid:
        first_day = min(s.session for s in valid)
        chosen = [s for s in valid if s.session == first_day]
        opens = {_aware(s.open_at, "calendar_open") for s in chosen}
        if len(opens) != 1 or any(s.open_at.astimezone(zone).date() != s.session for s in chosen):
            raise ValueError("ambiguous_exchange_session_calendar")
        return (next(iter(opens)).astimezone(timezone.utc),
            "exchange_session:" + ",".join(sorted({s.calendar_version for s in chosen})),
            sorted(s.id for s in chosen))
    following = public_day + timedelta(days=1)
    while following.weekday() >= 5:
        following += timedelta(days=1)
    return (datetime.combine(following, MARKETS[market][1], tzinfo=zone).astimezone(timezone.utc),
            "next_weekday_market_open_fallback", [])


def derive_daily_publication(document, filing, *, market, source_fact=None, metadata_records=(), sessions=()):
    """Resolve metadata only, with authoritative exact returned row identity."""
    if document.filing_id != filing.id or (source_fact is not None and (
            source_fact.document_id != document.id or source_fact.issuer_id != filing.issuer_id)):
        raise ValueError("daily_publication_source_identity_mismatch")
    version = source_version_ref(filing, source_fact)
    result = dict(status="unavailable", reason="publication_metadata_missing", policy_version=POLICY_VERSION,
        evidence_grade="metadata_grade", source_version_ref=version, market=market,
        public_day=None, effective_available_at=None, metadata_date_basis="unknown", calendar_basis="unknown",
        calendar_evidence_ids=[], original_archive_verified=False, metadata_refs=[])
    evidence = getattr(source_fact, "evidence", {}) or {}
    raw = evidence.get("source_row", {})
    filing_metadata = dict(getattr(filing, "evidence", {}) or {})
    identity_key = "rcept_no" if filing.source == "opendart" else "accessionNumber"
    if version == filing.accession and not any(filing_metadata.get(k) for k in ("rcept_no", "accn", "accessionNumber", "accession")):
        filing_metadata[identity_key] = filing.accession
    records = [raw, evidence] + list(metadata_records) + [filing_metadata]
    records += list((getattr(document, "inventory", {}) or {}).get("publication_metadata", []))
    matching, dates, correction_dates = [], set(), set()
    try:
        for entry in records:
            if not isinstance(entry, Mapping):
                continue
            entry = entry.get("filing", entry)
            identity = entry.get("rcept_no") if filing.source == "opendart" else entry.get("accn") or entry.get("accessionNumber") or entry.get("accession")
            if str(identity or "") != version:
                continue
            for key in ("correction_publication_date", "revision_public_day"):
                if entry.get(key):
                    correction_dates.add(_day(entry[key]))
            preferred = ("published_date", "public_day", "rcept_dt") if filing.source == "opendart" else ("published_date", "public_day", "filed", "filingDate", "filing_date")
            for key in preferred:
                if entry.get(key):
                    dates.add((_day(entry[key]), key))
                    matching.append(json_value(dict(entry)))
                    break
        is_replacement = getattr(document, "revision_kind", "original") == "replacement" or (getattr(document, "inventory", {}) or {}).get("correction_time_unknown")
        if is_replacement and version == filing.accession:
            if len(correction_dates) != 1:
                result["reason"] = "same_version_correction_day_unknown"
                return result
            public_day, basis = next(iter(correction_dates)), "explicit_correction_publication_date"
        elif dates:
            if len({day for day, _ in dates}) != 1:
                result["reason"] = "ambiguous_publication_metadata"
                return result
            public_day = next(iter(dates))[0]
            basis = "source_metadata:" + ",".join(sorted({key for _, key in dates}))
        elif filing.source == "opendart":
            public_day, basis = _day(version[:8]), "receipt_date_encoded_fallback"
        elif getattr(filing, "published_at", None) is not None and version == filing.accession:
            published = _aware(filing.published_at, "published_at")
            precision = getattr(filing, "publication_precision", None) or filing_metadata.get("publication_precision") or filing_metadata.get("precision")
            if precision in {"public_day", "filing_day", "date"}:
                public_day, basis = published.date(), "source_precision_public_day"
            else:
                public_day = published.astimezone(ZoneInfo(MARKETS[market][0])).date()
                basis = "source_published_timestamp_day"
        else:
            return result
        bound, calendar_basis, calendar_ids = next_market_session(public_day, market, sessions)
    except (ValueError, OverflowError, KeyError):
        result["reason"] = "invalid_publication_metadata_or_calendar"
        return result
    result.update(status="qualified", reason=None, public_day=public_day, effective_available_at=bound,
        metadata_date_basis=basis, calendar_basis=calendar_basis, calendar_evidence_ids=calendar_ids,
        metadata_refs=sorted(matching, key=stable_hash), metadata_public_bound=bound)
    return result


def append_daily_publication_evidence(session, *, document, filing, resolution, source_fact=None,
                                      captured_at=None, recorded_at=None):
    """Append one idempotent assertion without mutating any source clock."""
    captured_at = _aware(captured_at or utcnow(), "captured_at")
    recorded_at = _aware(recorded_at or utcnow(), "recorded_at")
    if recorded_at < captured_at or resolution.get("policy_version") != POLICY_VERSION:
        raise ValueError("daily_publication_clock_or_policy_mismatch")
    version = source_version_ref(filing, source_fact)
    if (resolution.get("source_version_ref") != version or resolution.get("evidence_grade") != "metadata_grade"
            or document.filing_id != filing.id or (source_fact is not None and (
                source_fact.document_id != document.id or source_fact.issuer_id != filing.issuer_id))):
        raise ValueError("daily_publication_source_binding_mismatch")
    if resolution.get("status") not in {"qualified", "unavailable"} or resolution.get("market") not in MARKETS:
        raise ValueError("daily_publication_status_or_market_mismatch")
    if resolution["status"] == "qualified":
        day = resolution.get("public_day")
        bound = _aware(resolution.get("effective_available_at"), "qualified_public_bound")
        if type(day) is not date or bound.astimezone(ZoneInfo(MARKETS[resolution["market"]][0])).date() <= day:
            raise ValueError("daily_publication_next_session_bound_required")
    values = dict(document_id=document.id, source_fact_id=source_fact.id if source_fact is not None else None,
        filing_id=filing.id, issuer_id=filing.issuer_id, source=filing.source, market=resolution["market"],
        source_version_ref=version, status=resolution["status"], reason=resolution.get("reason"),
        public_day=resolution["public_day"], effective_available_at=resolution["effective_available_at"],
        evidence_grade="metadata_grade", metadata_date_basis=resolution["metadata_date_basis"],
        calendar_basis=resolution["calendar_basis"], policy_version=POLICY_VERSION,
        evidence=json_value(resolution))
    signature = stable_hash(values)
    session.execute(insert(FinancialDailyPublicationEvidence).values(id=new_id(), assertion_hash=signature,
        captured_at=captured_at, recorded_at=recorded_at, **values).on_conflict_do_nothing(
            index_elements=[FinancialDailyPublicationEvidence.assertion_hash]))
    return session.scalar(select(FinancialDailyPublicationEvidence).where(FinancialDailyPublicationEvidence.assertion_hash == signature))


def resolve_daily_availability(document, filing, evidence, *, source_fact=None, as_of, history_mode="reconstructed_public"):
    """Read exact document/fact-bound assertions, retaining physical clocks."""
    _aware(as_of, "as_of")
    if history_mode not in {"reconstructed_public", "observed_system"}:
        raise ValueError("unsupported_history_mode")
    version = source_version_ref(filing, source_fact)
    candidates = []
    for item in evidence:
        if (item.document_id != document.id or item.filing_id != filing.id or item.issuer_id != filing.issuer_id
                or item.source != filing.source or item.policy_version != POLICY_VERSION
                or item.source_version_ref != version or item.status != "qualified"
                or item.evidence_grade != "metadata_grade"):
            continue
        if item.source_fact_id is not None and (source_fact is None or item.source_fact_id != source_fact.id):
            continue
        if history_mode == "observed_system" and max(item.captured_at, item.recorded_at, document.captured_at, filing.captured_at,
                getattr(source_fact, "captured_at", item.captured_at)) > as_of:
            continue
        candidates.append(item)
    # Specific returned-identity assertions supersede document-wide metadata.
    specific = [x for x in candidates if x.source_fact_id is not None]
    candidates = specific or candidates
    if not candidates:
        return dict(status="unavailable", reason="daily_publication_metadata_unavailable", effective_available_at=None,
                    evidence_ids=[], policy_version=POLICY_VERSION, evidence_grade="metadata_grade")
    bounds = {(x.public_day, x.effective_available_at) for x in candidates}
    if len(bounds) != 1:
        return dict(status="unavailable", reason="ambiguous_daily_publication_assertions", effective_available_at=None,
                    evidence_ids=[], policy_version=POLICY_VERSION, evidence_grade="metadata_grade")
    chosen = min(candidates, key=lambda x: x.id)
    result = dict(chosen.evidence)
    result.update(public_day=chosen.public_day, effective_available_at=chosen.effective_available_at,
                  metadata_public_bound=chosen.effective_available_at, evidence_ids=sorted(x.id for x in candidates))
    if chosen.effective_available_at > as_of:
        result.update(status="unavailable", reason="future_publication_metadata_bound")
    return result


def select_daily_facts_asof(facts, *, as_of, history_mode="reconstructed_public"):
    """Public-day revision order; equal-day conflicting context values reject."""
    groups = {}
    for fact in facts:
        availability = fact.evidence.get("public_availability", {})
        if availability.get("policy_version") != POLICY_VERSION or availability.get("status") != "qualified":
            continue
        if fact_visible(fact, as_of=as_of, history_mode=history_mode):
            groups.setdefault(fact_identity(fact), []).append(fact)
    result = []
    for versions in groups.values():
        def public_rank(fact):
            availability = fact.evidence.get("public_availability", {})
            try:
                return _day(availability.get("public_day")), fact.available_at
            except ValueError:
                raise AmbiguousFinancialRevision("unknown_daily_publication_context_day") from None
        latest_day = max(public_rank(f)[0] for f in versions)
        latest_versions = [f for f in versions if public_rank(f)[0] == latest_day]
        if any(f.value != latest_versions[0].value for f in latest_versions):
            raise AmbiguousFinancialRevision("conflicting_daily_publication_context")
        latest = max(public_rank(f) for f in latest_versions)
        winning = [f for f in latest_versions if public_rank(f) == latest]
        if any(f.value != winning[0].value for f in winning):
            raise AmbiguousFinancialRevision("conflicting_daily_publication_context")
        result.append(replace(min(winning, key=lambda f: f.id), revision=0))
    return tuple(result)
