"""Versioned two-axis classification on the unchanged provisional weekday grid."""
from collections import Counter, defaultdict
from datetime import date, datetime

from .policy import provisional_weekdays, validate_bounds

POLICY_VERSION = "coverage_eligibility_v1"
PROFILES = ("qualified", "provider_declared_research")
CATEGORIES = ("verified_closed", "reported_closed", "verified_not_listed", "reported_not_listed",
    "verified_eligible_clean", "verified_eligible_missing", "reported_eligible_clean",
    "reported_eligible_missing", "unknown", "conflict")


def exact_day(value):
    if type(value) is date:
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError("requires an exact date")


def aware_clock(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evidence requires an aware timestamp")
    return value


def normalize_calendar_rows(rows, *, start, end, complete_interval=False):
    """Absence means closed only under an explicit, nonempty complete response."""
    start, end = exact_day(start), exact_day(end)
    if start > end or (end - start).days > 20 * 366:
        raise ValueError("calendar interval must be ordered and bounded")
    if not isinstance(rows, (list, tuple)) or len(rows) > 10000:
        raise ValueError("calendar rows must be bounded")
    if complete_interval and not rows:
        raise ValueError("empty response cannot prove a complete calendar")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("calendar row must be a normalized object")
        day = exact_day(row.get("date", row.get("session")))
        status = row.get("status", "open")
        if not start <= day <= end or day in seen or status not in ("open", "closed"):
            raise ValueError("calendar row must have a unique in-range date and explicit status")
        seen.add(day)
        result.append({**row, "date": day.isoformat(), "status": status})
    return sorted(result, key=lambda row: row["date"])


def _value(item, key, default=None):
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _visible(item, cutoff):
    return (_value(item, "status") == "accepted" and
        aware_clock(_value(item, "captured_at")) <= cutoff and
        aware_clock(_value(item, "recorded_at")) <= cutoff)


def _resolve_axis(assertions):
    """Keep disagreements at all accepted grades; stronger evidence cannot hide them."""
    if not assertions:
        return "unknown", []
    states = {state for state, _, _ in assertions}
    refs = sorted({ref for _, _, ref in assertions})
    if len(states) > 1:
        return "conflict", refs
    grade = "verified" if any(verified for _, verified, _ in assertions) else "reported"
    return grade + "_" + next(iter(states)), refs


def evaluate_eligibility(*, start, end, security_id, market, calendars=(), listings=(),
        evidence_cutoff, profile="provider_declared_research", clean=(), observed=()):
    """Classify each provisional position exactly once; never change baseline quotes."""
    if profile not in PROFILES:
        raise ValueError("unsupported eligibility profile")
    validate_bounds(start, end, evidence_cutoff)
    grid = provisional_weekdays(start, end)
    clean, observed = set(clean), set(observed) | set(clean)
    if not clean.issubset(set(grid)):
        raise ValueError("clean dates must belong to the provisional grid")
    calendar_rows = []
    for item in calendars:
        if not _visible(item, evidence_cutoff) or _value(item, "market") != market:
            continue
        verified = bool(_value(item, "authoritative"))
        if profile == "qualified" and not verified:
            continue
        rows = {exact_day(row["date"]): row["status"] for row in _value(item, "rows", [])}
        calendar_rows.append((item, rows, verified))
    listing_groups = defaultdict(list)
    for item in listings:
        if not _visible(item, evidence_cutoff) or _value(item, "security_id") != security_id:
            continue
        identity = _value(item, "identity_status")
        verified = identity == "verified_security" and _value(item, "grade") == "verified"
        if identity not in ("verified_security", "source_declared_security") or (profile == "qualified" and not verified):
            continue
        # Union only intervals in one original declaration/capture set. A shared
        # lifecycle alone must not merge contradictory snapshots or providers.
        declaration = _value(item, "source_refs", {}).get("lifecycle_declaration_ref")
        grouping = ((_value(item, "lifecycle_ref"), _value(item, "source"),
            _value(item, "captured_at"), declaration) if declaration else _value(item, "assertion_hash"), verified)
        listing_groups[grouping].append(item)
    counts = Counter({category: 0 for category in CATEGORIES})
    calendar_counts, listing_counts = Counter(), Counter()
    spans = []
    for day in grid:
        cal_assertions, list_assertions = [], []
        for item, rows, verified in calendar_rows:
            if not exact_day(_value(item, "start")) <= day <= exact_day(_value(item, "end")):
                continue
            status = rows.get(day)
            if status is None and _value(item, "complete_interval"):
                status = "closed"
            if status is not None:
                cal_assertions.append((status, verified, _value(item, "assertion_hash")))
        for (_, verified), group in listing_groups.items():
            applicable = [item for item in group if exact_day(_value(item, "scope_start")) <= day <= exact_day(_value(item, "scope_end"))]
            if not applicable:
                continue
            listed = any(exact_day(_value(item, "listing_start")) <= day and
                (_value(item, "listing_end") is None or day < exact_day(_value(item, "listing_end"))) for item in applicable)
            complete = any(_value(item, "complete_history") for item in applicable)
            declared_before = not verified and any(_value(item, "initial_listing_declared") and
                day < exact_day(_value(item, "listing_start")) for item in applicable)
            declared_after = not verified and any(_value(item, "listing_end") is not None and
                day >= exact_day(_value(item, "listing_end")) for item in applicable)
            if listed or complete or declared_before or declared_after:
                for item in applicable:
                    list_assertions.append(("listed" if listed else "not_listed", verified, _value(item, "assertion_hash")))
        cal, cal_refs = _resolve_axis(cal_assertions)
        listed, list_refs = _resolve_axis(list_assertions)
        calendar_counts[cal] += 1
        listing_counts[listed] += 1
        conflict = cal == "conflict" or listed == "conflict" or (day in observed and
            (cal.endswith("_closed") or listed.endswith("_not_listed")))
        if conflict:
            category = "conflict"
        elif cal.endswith("_closed"):
            category = cal
        elif listed.endswith("_not_listed"):
            category = listed
        elif cal.endswith("_open") and listed.endswith("_listed"):
            grade = "verified" if cal.startswith("verified_") and listed.startswith("verified_") else "reported"
            category = grade + ("_eligible_clean" if day in clean else "_eligible_missing")
        else:
            category = "unknown"
        counts[category] += 1
        signature = (category, cal, listed, tuple(cal_refs), tuple(list_refs), day in clean, day in observed)
        if not spans or spans[-1]["_signature"] != signature:
            spans.append({"start": day.isoformat(), "end": day.isoformat(), "count": 0,
                "category": category, "calendar": cal, "listing": listed, "clean_count": 0,
                "observed_count": 0, "calendar_refs": cal_refs, "listing_refs": list_refs,
                "dates": [], "_signature": signature})
        span = spans[-1]
        span["end"] = day.isoformat()
        span["dates"].append(day.isoformat())
        span["count"] += 1
        span["clean_count"] += int(day in clean)
        span["observed_count"] += int(day in observed)
    for span in spans:
        del span["_signature"]
    return {"policy_version": POLICY_VERSION, "profile": profile, "provisional_count": len(grid),
        "baseline_clean_count": len(clean), "baseline_missing_count": len(grid) - len(clean),
        "verified_expected_count": counts["verified_eligible_clean"] + counts["verified_eligible_missing"],
        "reported_expected_count": counts["reported_eligible_clean"] + counts["reported_eligible_missing"],
        "counts": dict(counts), "calendar_counts": dict(calendar_counts),
        "listing_counts": dict(listing_counts), "spans": spans, "historical_universe_certified": False}
