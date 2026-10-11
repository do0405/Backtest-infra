"""Conservative comparison policy without invented calendar or fiscal evidence."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


POLICY_VERSION = "whole_target_coverage_v1"
EXPECTED_MARKET_COUNTS = {"KR": 4507, "US": 13251}


def validate_bounds(start, end, watermark):
    if type(start) is not date or type(end) is not date or start > end:
        raise ValueError("coverage interval requires ordered exact dates")
    if not isinstance(watermark, datetime) or watermark.tzinfo is None or watermark.utcoffset() is None:
        raise ValueError("coverage watermark requires an aware timestamp")
    if end > watermark.astimezone(timezone.utc).date():
        raise ValueError("coverage cannot include future dates")
    if (end - start).days > 20 * 366:
        raise ValueError("coverage interval must be bounded to twenty years")


def provisional_weekdays(start, end):
    """Weekdays include holidays; no claim of official exchange sessions."""
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1)
        if (start + timedelta(days=offset)).weekday() < 5)


def missing_date_spans(expected, observed):
    """Compress adjacent missing positions on the declared provisional grid."""
    result, active = [], None
    for day in expected:
        if day in observed:
            active = None
        elif active is None:
            active = {"start": day.isoformat(), "end": day.isoformat(), "count": 1}
            result.append(active)
        else:
            active["end"] = day.isoformat()
            active["count"] += 1
    return result


def company_fundamentals_applicability(kind):
    kind = str(kind or "").casefold()
    if kind in {"etf", "etn"}:
        return "not_applicable"
    if kind in {"common", "common_stock", "equity", "preferred", "adr"}:
        return "applicable"
    return "unknown"


def market_cutoff(end, market):
    zone = ZoneInfo({"KR": "Asia/Seoul", "US": "America/New_York"}.get(market, "UTC"))
    return datetime.combine(end + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc) - timedelta(microseconds=1)
