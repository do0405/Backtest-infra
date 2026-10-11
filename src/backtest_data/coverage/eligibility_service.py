"""Bounded, atomic eligibility materialization over immutable baseline summaries.

Callers own the surrounding commit. Savepoints keep imports/pages atomic even
when a caller handles an exception and continues using its transaction.
"""
import base64
import binascii
import json
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from ..catalog.models import Security
from ..db import new_id, utcnow
from ..fundamentals.domain import stable_hash
from ..prices.models import PriceObservation, VWAPDailyStat
from .eligibility import (POLICY_VERSION, PROFILES, aware_clock, evaluate_eligibility,
    exact_day, normalize_calendar_rows)
from .eligibility_models import CalendarEligibilityEvidence, EligibilityOverlay, EligibilityOverlayPage, ListingEligibilityEvidence
from .models import CoverageMember, CoverageRun, CoverageSummary
from .policy import POLICY_VERSION as BASELINE_POLICY_VERSION, provisional_weekdays

MAX_EVIDENCE_ROWS = 1000
REJECTED_LISTING_ISSUES = {"ipo_date_after_snapshot", "delisting_date_before_ipo_date",
    "delisting_date_after_snapshot", "active_status_after_delisting_date", "future_delisting_date",
    "listing_date_after_requested_snapshot", "listing_date_missing_or_invalid", "source_isin_format"}


def _clocks(captured_at, recorded_at):
    captured_at = aware_clock(captured_at).astimezone(timezone.utc)
    recorded_at = aware_clock(recorded_at or utcnow()).astimezone(timezone.utc)
    if recorded_at < captured_at:
        raise ValueError("recording cannot precede evidence capture")
    return captured_at, recorded_at


def _source_refs(refs):
    if not isinstance(refs, dict) or not refs or not any(refs.get(key) for key in
            ("body_hash", "content_hash", "raw_id", "document_id", "observation_id", "capture_id")):
        raise ValueError("immutable raw or capture source references required")
    # Reject non-JSON values before entering a database transaction.
    json.dumps(refs, sort_keys=True)
    return dict(refs)


def _append(session, model, values):
    assertion_hash = stable_hash({key: value for key, value in values.items() if key != "recorded_at"})
    statement = insert(model).values(id=new_id(), assertion_hash=assertion_hash, **values)
    session.execute(statement.on_conflict_do_nothing(index_elements=[model.assertion_hash]))
    return session.scalar(select(model).where(model.assertion_hash == assertion_hash))


def import_calendar_evidence(session, *, market, source, calendar_version, returned_rows,
        start, end, captured_at, source_refs, complete_interval=False, authoritative=False,
        recorded_at=None):
    """Import a deterministic normalized response; this function makes no API calls.

    complete_interval must describe the source response contract, never an
    inference from quote absence. Broker calendars remain provider declarations.
    """
    if market not in ("KR", "US") or not source or not calendar_version:
        raise ValueError("calendar market/source/version required")
    if type(complete_interval) is not bool or type(authoritative) is not bool:
        raise ValueError("calendar declaration flags must be booleans")
    source_refs = _source_refs(source_refs)
    if authoritative and not (source_refs.get("authority") == "official_exchange" and
            source_refs.get("calendar_scope_verified") is True):
        raise ValueError("authoritative calendar requires explicit official exchange scope proof")
    start, end = exact_day(start), exact_day(end)
    rows = normalize_calendar_rows(returned_rows, start=start, end=end, complete_interval=complete_interval)
    captured_at, recorded_at = _clocks(captured_at, recorded_at)
    values = dict(market=market, source=source, calendar_version=calendar_version,
        start=start, end=end, rows=rows, complete_interval=complete_interval, authoritative=authoritative,
        status="accepted", captured_at=captured_at, recorded_at=recorded_at,
        policy_version=POLICY_VERSION, source_refs=source_refs)
    with session.begin_nested():
        return _append(session, CalendarEligibilityEvidence, values)


def import_listing_evidence(session, *, security_id, market, source, listing_start, listing_end,
        scope_start, scope_end, captured_at, source_refs, identity_status="unresolved_symbol",
        grade="source_declared", complete_history=False, initial_listing_declared=False,
        lifecycle_ref=None, evidence=None, recorded_at=None):
    """Append one security-specific half-open listing interval with explicit scope.

    Current aliases/names are insufficient for verified history. complete_history
    means the declared lifecycle group contains all intervals over its scope.
    """
    listing_start = exact_day(listing_start)
    listing_end = exact_day(listing_end) if listing_end is not None else None
    scope_start, scope_end = exact_day(scope_start), exact_day(scope_end)
    if scope_start > scope_end or (scope_end - scope_start).days > 20 * 366:
        raise ValueError("listing scope must be ordered and bounded")
    if listing_end is not None and listing_end <= listing_start:
        raise ValueError("listing end must follow listing start")
    if identity_status not in ("verified_security", "source_declared_security", "unresolved_symbol"):
        raise ValueError("unsupported listing identity status")
    if grade not in ("verified", "source_declared"):
        raise ValueError("unsupported listing evidence grade")
    if type(complete_history) is not bool or type(initial_listing_declared) is not bool:
        raise ValueError("listing declaration flags must be booleans")
    source_refs = _source_refs(source_refs)
    evidence = dict(evidence or {})
    json.dumps(evidence, sort_keys=True)
    captured_at, recorded_at = _clocks(captured_at, recorded_at)
    reasons = []
    if evidence.get("snapshot_scope_valid") is False or evidence.get("fields", {}).get("snapshot_scope_valid") is False:
        reasons.append("contradictory_snapshot_scope")
    issue_rules = {issue if isinstance(issue, str) else issue.get("rule")
        for issue in evidence.get("issues", []) if isinstance(issue, (str, dict))}
    if REJECTED_LISTING_ISSUES.intersection(issue_rules):
        reasons.append("contradictory_listing_dates")
    if str(evidence.get("form", "")).upper().startswith("25") and listing_end and not evidence.get("effective_delisting_date_verified"):
        reasons.append("form25_filing_is_not_effective_delisting")
    binding = evidence.get("security_binding", {})
    if identity_status != "unresolved_symbol" and binding.get("security_id") != security_id:
        reasons.append("security_binding_missing")
    if identity_status == "verified_security" or grade == "verified":
        if not (identity_status == "verified_security" and grade == "verified" and
                evidence.get("historical_identity_verified") is True and
                evidence.get("listing_interval_verified") is True and binding.get("basis") == "historical_security_identity"):
            reasons.append("historical_security_interval_proof_missing")
    if complete_history and (not lifecycle_ref or not source_refs.get("lifecycle_declaration_ref")):
        reasons.append("complete_history_requires_original_declaration_set")
    with session.begin_nested():
        security = session.get(Security, security_id)
        if security is None or security.market != market:
            raise ValueError("listing security/market binding not found")
        return _append(session, ListingEligibilityEvidence, dict(security_id=security_id,
            market=market, source=source, listing_start=listing_start, listing_end=listing_end,
            scope_start=scope_start, scope_end=scope_end, complete_history=complete_history,
            initial_listing_declared=initial_listing_declared, lifecycle_ref=lifecycle_ref,
            identity_status=identity_status, grade=grade, status="rejected" if reasons else "accepted",
            captured_at=captured_at, recorded_at=recorded_at, policy_version=POLICY_VERSION,
            source_refs=source_refs, evidence={**evidence, "rejection_reasons": reasons}))


def import_krx_listing_evidence(session, *, observation_id, security_id, source_isin,
        scope_start=date(2016, 1, 1), recorded_at=None):
    """Explicit ISIN binding supplies a source-declared annotation, never history grade."""
    from ..prices.krx_quote_models import KRXQuoteObservation
    row = session.get(KRXQuoteObservation, observation_id)
    if row is None or not source_isin or row.source_isin != source_isin or row.listing_date is None:
        raise ValueError("KRX listing observation and explicit matching source ISIN required")
    if row.listing_date > row.request_date:
        raise ValueError("KRX listing date follows its source snapshot")
    return import_listing_evidence(session, security_id=security_id, market="KR", source="krx",
        listing_start=row.listing_date, listing_end=None, scope_start=scope_start, scope_end=row.request_date,
        captured_at=row.captured_at, recorded_at=recorded_at,
        source_refs={"observation_id": row.id, "raw_id": row.raw_id, "capture_id": row.response_capture_id,
            "source_contract_id": row.source_contract_id, "row_signature": row.signature},
        identity_status="source_declared_security", grade="source_declared", complete_history=False,
        initial_listing_declared=True, evidence={"request_date": row.request_date.isoformat(),
            "source_isin": row.source_isin, "listing_date": row.listing_date.isoformat(), "issues": row.issues,
            "security_binding": {"security_id": security_id, "basis": "source_isin_at_request"},
            "historical_identity_verified": False})


def import_supplemental_listing_evidence(session, *, observation_id, security_id,
        source_declared_binding=False, scope_start=date(2016, 1, 1), recorded_at=None):
    """Retain Alpha date assertions and issues without upgrading current symbol matches."""
    from ..supplemental.models import SupplementalDocument, SupplementalObservation
    row = session.get(SupplementalObservation, observation_id)
    if row is None or row.ipo_date is None or row.report_date is None:
        raise ValueError("supplemental listing observation requires IPO and report dates")
    document = session.get(SupplementalDocument, row.document_id)
    if document is None:
        raise ValueError("supplemental source document not found")
    return import_listing_evidence(session, security_id=security_id, market="US", source=document.source,
        listing_start=row.ipo_date, listing_end=row.delisting_date, scope_start=scope_start, scope_end=row.report_date,
        captured_at=max(row.captured_at, document.captured_at), recorded_at=recorded_at,
        source_refs={"observation_id": row.id, "document_id": document.id, "raw_id": document.raw_id,
            "body_hash": document.content_hash, "source_url": document.source_url},
        identity_status="source_declared_security" if source_declared_binding else "unresolved_symbol",
        grade="source_declared", complete_history=False, initial_listing_declared=True,
        evidence={"fields": row.fields, "issues": row.issues, "report_date": row.report_date.isoformat(),
            "security_binding": {"security_id": security_id, "basis": "declared_current_symbol"},
            "historical_identity_verified": False})


def _validate_limit(limit):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("eligibility limit must be an integer from 1 to 100")


def _cursor(overlay_id, ordinal):
    payload = json.dumps({"overlay_id": overlay_id, "ordinal": ordinal}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _parse_cursor(cursor):
    try:
        if not isinstance(cursor, str) or len(cursor) > 256:
            raise ValueError()
        value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        if set(value) != {"overlay_id", "ordinal"} or not isinstance(value["overlay_id"], str) or type(value["ordinal"]) is not int or value["ordinal"] < 1:
            raise ValueError()
        return value["overlay_id"], value["ordinal"]
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise ValueError("invalid eligibility cursor") from None


def _metadata(overlay):
    return {"overlay_id": overlay.id, "run_id": overlay.run_id, "profile": overlay.profile,
        "policy_version": overlay.policy_version, "baseline_manifest_hash": overlay.baseline_manifest_hash,
        "baseline_watermark": overlay.baseline_watermark.isoformat(), "baseline_target_count": overlay.baseline_target_count,
        "output_start": overlay.output_start.isoformat(), "output_end": overlay.output_end.isoformat(),
        "evidence_cutoff": overlay.evidence_cutoff.isoformat(), "snapshot_hash": overlay.snapshot_hash,
        "baseline_preserved": True, "historical_universe_certified": False}


def _page_response(overlay, page):
    return {**_metadata(overlay), "start_cursor": page.start_cursor, "end_cursor": page.end_cursor,
        "page_member_count": len(page.members), "complete": page.complete, "evidence_hash": page.evidence_hash,
        "content_hash": page.content_hash, "previous_hash": page.previous_hash, "members": page.members,
        "next_cursor": None if page.complete else _cursor(overlay.id, page.end_cursor)}


def _bounded_evidence(session, statement):
    rows = list(session.scalars(statement.limit(MAX_EVIDENCE_ROWS + 1)))
    if len(rows) > MAX_EVIDENCE_ROWS:
        raise ValueError("eligibility evidence bound exceeded; narrow the evidence scope")
    return rows


def _baseline_clean(summary, grid):
    if summary is None or summary.expected_count is None:
        return set(), "baseline_unresolved_security"
    if summary.details.get("expected_basis") != "provisional_weekdays" or summary.expected_count != len(grid):
        raise ValueError("baseline provisional grid contract mismatch")
    missing = set()
    for span in summary.details.get("missing_date_spans", []):
        left = bisect_left(grid, exact_day(span["start"]))
        right = bisect_right(grid, exact_day(span["end"]))
        values = set(grid[left:right])
        if len(values) != span["count"] or missing.intersection(values):
            raise ValueError("baseline missing spans do not conserve positions")
        missing.update(values)
    clean = set(grid) - missing
    if len(missing) != summary.missing_count or len(clean) > summary.clean_count or summary.clean_count > summary.observed_count:
        raise ValueError("baseline stored counts and spans disagree")
    return clean, "stored_baseline_summary"


def _observed_dates(session, run, security_ids, model):
    result = defaultdict(set)
    if security_ids:
        statement = select(model.security_id, model.session).where(model.security_id.in_(security_ids),
            model.session >= run.output_start, model.session <= run.output_end,
            model.captured_at <= run.watermark).distinct()
        # The selected securities/date interval bound output, independent of
        # capture multiplicity. Only date/index fields are read, never raw blobs.
        for security_id, day in session.execute(statement):
            result[security_id].add(day)
    return result


def materialize_eligibility_overlay(session, run_id, limit=25, cursor=None,
        evidence_cutoff=None, profile="provider_declared_research"):
    """Materialize one immutable page; caller commits page and its cursor together.

    Resume with the returned opaque cursor and the original profile/cutoff/limit.
    Replay of an already stored page returns its exact stored result.
    """
    _validate_limit(limit)
    if profile not in PROFILES:
        raise ValueError("unsupported eligibility profile")
    cursor_overlay, ordinal = _parse_cursor(cursor) if cursor else (None, 0)
    if evidence_cutoff is not None:
        evidence_cutoff = aware_clock(evidence_cutoff).astimezone(timezone.utc)
    elif cursor is None:
        raise ValueError("first eligibility page requires an explicit evidence cutoff")
    with session.begin_nested():
        run = session.get(CoverageRun, run_id)
        if run is None or run.policy_version != BASELINE_POLICY_VERSION:
            raise ValueError("supported baseline coverage run required")
        if cursor_overlay:
            overlay = session.scalar(select(EligibilityOverlay).where(EligibilityOverlay.id == cursor_overlay).with_for_update())
            if overlay is None or overlay.run_id != run_id or overlay.profile != profile or (
                    evidence_cutoff is not None and overlay.evidence_cutoff != evidence_cutoff):
                raise ValueError("cursor must resume its original run/profile/evidence cutoff")
        else:
            if evidence_cutoff < run.watermark:
                raise ValueError("evidence cutoff cannot precede baseline watermark")
            values = dict(run_id=run.id, baseline_manifest_hash=run.manifest_hash,
                baseline_watermark=run.watermark, baseline_target_count=run.target_count,
                output_start=run.output_start, output_end=run.output_end, evidence_cutoff=evidence_cutoff,
                profile=profile, policy_version=POLICY_VERSION)
            snapshot_hash = stable_hash(values)
            statement = insert(EligibilityOverlay).values(id=new_id(), snapshot_hash=snapshot_hash, **values)
            session.execute(statement.on_conflict_do_nothing(index_elements=[EligibilityOverlay.snapshot_hash]))
            overlay = session.scalar(select(EligibilityOverlay).where(EligibilityOverlay.snapshot_hash == snapshot_hash).with_for_update())
        page = session.scalar(select(EligibilityOverlayPage).where(EligibilityOverlayPage.overlay_id == overlay.id,
            EligibilityOverlayPage.start_cursor == ordinal))
        if page is not None:
            if page.member_limit != limit:
                raise ValueError("replay must retain its original page limit")
            return _page_response(overlay, page)
        previous_hash = overlay.snapshot_hash
        if ordinal:
            previous = session.scalar(select(EligibilityOverlayPage).where(EligibilityOverlayPage.overlay_id == overlay.id,
                EligibilityOverlayPage.end_cursor == ordinal).limit(1))
            if previous is None or previous.complete:
                raise ValueError("cursor must reference a committed incomplete page")
            previous_hash = previous.content_hash
        members = list(session.scalars(select(CoverageMember).where(CoverageMember.run_id == run.id,
            CoverageMember.ordinal > ordinal).order_by(CoverageMember.ordinal).limit(limit)))
        if not members or any(not member.done for member in members):
            raise ValueError("baseline page is not audited yet")
        if members[0].ordinal != ordinal + 1 or any(right.ordinal != left.ordinal + 1 for left, right in zip(members, members[1:])):
            raise ValueError("baseline membership cursor is not contiguous")
        ids = {member.security_id for member in members if member.security_id}
        member_ids = [member.id for member in members]
        summaries = {(row.member_id, row.domain): row for row in session.scalars(select(CoverageSummary).where(
            CoverageSummary.member_id.in_(member_ids), CoverageSummary.domain.in_(("prices", "vwap"))))}
        calendars = _bounded_evidence(session, select(CalendarEligibilityEvidence).where(
            CalendarEligibilityEvidence.market.in_({member.market for member in members}),
            CalendarEligibilityEvidence.start <= overlay.output_end, CalendarEligibilityEvidence.end >= overlay.output_start,
            CalendarEligibilityEvidence.captured_at <= overlay.evidence_cutoff,
            CalendarEligibilityEvidence.recorded_at <= overlay.evidence_cutoff,
            CalendarEligibilityEvidence.policy_version == POLICY_VERSION).order_by(CalendarEligibilityEvidence.assertion_hash))
        listings = _bounded_evidence(session, select(ListingEligibilityEvidence).where(
            ListingEligibilityEvidence.security_id.in_(ids), ListingEligibilityEvidence.scope_start <= overlay.output_end,
            ListingEligibilityEvidence.scope_end >= overlay.output_start,
            ListingEligibilityEvidence.captured_at <= overlay.evidence_cutoff,
            ListingEligibilityEvidence.recorded_at <= overlay.evidence_cutoff,
            ListingEligibilityEvidence.policy_version == POLICY_VERSION).order_by(ListingEligibilityEvidence.assertion_hash))
        observed = {"prices": _observed_dates(session, run, ids, PriceObservation),
            "vwap": _observed_dates(session, run, ids, VWAPDailyStat)}
        grid = provisional_weekdays(overlay.output_start, overlay.output_end)
        results = []
        for member in members:
            domains = {}
            for domain in ("prices", "vwap"):
                summary = summaries.get((member.id, domain))
                clean, baseline_basis = _baseline_clean(summary, grid)
                result = evaluate_eligibility(start=overlay.output_start, end=overlay.output_end,
                    security_id=member.security_id, market=member.market, calendars=calendars, listings=listings,
                    evidence_cutoff=overlay.evidence_cutoff, profile=overlay.profile, clean=clean,
                    observed=observed[domain].get(member.security_id, set()))
                domains[domain] = {**result, "baseline_basis": baseline_basis,
                    "baseline_stored_expected_count": summary.expected_count if summary else None,
                    "baseline_stored_observed_count": summary.observed_count if summary else None,
                    "baseline_stored_clean_count": summary.clean_count if summary else None,
                    "baseline_stored_missing_count": summary.missing_count if summary else None,
                    "baseline_clean_outside_grid_count": summary.clean_count - len(clean) if summary and summary.expected_count is not None else None}
            results.append({"member_id": member.id, "ordinal": member.ordinal,
                "security_id": member.security_id, "market": member.market, "domains": domains})
        evidence_hash = stable_hash({"calendar": sorted(row.assertion_hash for row in calendars),
            "listing": sorted(row.assertion_hash for row in listings), "cutoff": overlay.evidence_cutoff})
        end_cursor = members[-1].ordinal
        complete = end_cursor == overlay.baseline_target_count
        content_hash = stable_hash({"previous_hash": previous_hash, "evidence_hash": evidence_hash,
            "members": results, "start_cursor": ordinal, "end_cursor": end_cursor, "complete": complete})
        page = EligibilityOverlayPage(overlay_id=overlay.id, start_cursor=ordinal, end_cursor=end_cursor,
            member_limit=limit, previous_hash=previous_hash, evidence_hash=evidence_hash,
            content_hash=content_hash, complete=complete, members=results)
        session.add(page)
        session.flush()
        return _page_response(overlay, page)


def eligibility_overlay_report(session, overlay_id, *, limit=25, cursor=None):
    """Read at most 100 stored members using a bounded JSON page query."""
    _validate_limit(limit)
    ordinal = 0
    if cursor:
        cursor_id, ordinal = _parse_cursor(cursor)
        if cursor_id != overlay_id:
            raise ValueError("report cursor belongs to another overlay")
    overlay = session.get(EligibilityOverlay, overlay_id)
    if overlay is None:
        raise ValueError("eligibility overlay not found")
    # Restrict pages before expanding JSON. A page holds at most 100 members;
    # LIMIT 100 bounds page reads even when the target contains 17,758 members.
    statement = text("""SELECT expanded.value FROM
        (SELECT members FROM coverage_eligibility_page
         WHERE overlay_id=:overlay_id AND end_cursor>:ordinal
         ORDER BY start_cursor LIMIT 100) AS page,
        LATERAL jsonb_array_elements(page.members) AS expanded(value)
        WHERE (expanded.value->>'ordinal')::integer>:ordinal
        ORDER BY (expanded.value->>'ordinal')::integer LIMIT :row_limit""")
    rows = list(session.scalars(statement, {"overlay_id": overlay_id, "ordinal": ordinal, "row_limit": limit + 1}))
    more = len(rows) > limit
    rows = rows[:limit]
    latest = session.scalar(select(EligibilityOverlayPage).where(EligibilityOverlayPage.overlay_id == overlay_id)
        .order_by(EligibilityOverlayPage.end_cursor.desc()).limit(1))
    # The inner page bound can hide the lookahead row when 100 pages each hold
    # one member. The materialized ordinal proves continuation without reading
    # more pages or scanning the full original target.
    more = more or bool(rows and latest and latest.end_cursor > rows[-1]["ordinal"])
    return {**_metadata(overlay), "members": rows, "page_member_count": len(rows),
        "materialized_through_ordinal": latest.end_cursor if latest else 0,
        "complete": bool(latest and latest.complete), "latest_content_hash": latest.content_hash if latest else None,
        "next_cursor": _cursor(overlay_id, rows[-1]["ordinal"]) if more and rows else None}
