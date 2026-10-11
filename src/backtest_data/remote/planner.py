"""Whole-universe refresh policy and explicit account ownership admission."""
from datetime import timedelta
from .codec import dumps

CORE_SOURCES = {"sec", "opendart", "kis", "kis_overseas", "alpaca"}
SOURCES = CORE_SOURCES | {'krx','supplemental','alpha_vantage'}


def validate_ownership(configuration):
    if configuration.get("enabled") is not True: raise ValueError("remote acquisition disabled until ownership is configured")
    owner = configuration.get("owner_id")
    slots = configuration.get("provider_slots", [])
    if not owner or not slots: raise ValueError("explicit remote account ownership required")
    accounts = {}
    for slot in slots:
        if slot.get("source") not in SOURCES or not slot.get("account_id"):
            raise ValueError("registered source and opaque account identity required")
        if slot.get("owner") != "remote" or slot.get("local_enabled") is not False:
            raise ValueError("local account acquisition must be disabled before remote ownership")
        family = "kis" if slot["source"] == "kis_overseas" else slot["source"]
        key = (family, slot["account_id"])
        if key in accounts and accounts[key] != slot.get("owner"): raise ValueError("provider account ownership conflict")
        accounts[key] = slot["owner"]
        if slot.get("market") not in ("us", "kr", "both") or slot.get("domain") not in ("prices", "fundamentals"):
            raise ValueError("explicit acquisition market/domain required")
    if configuration.get('identity_handoff_ready') is not True:
        raise ValueError('existing source identity handoff is not ready for remote acquisition')
    return slots


def refresh_plan(scope, *, as_of, recent_sessions):
    if scope.get("target_count") != 17758 or scope.get("output_start") != "2016-01-01" or scope.get("output_end") != "2026-10-07":
        raise ValueError("whole original target scope required")
    dumps(as_of)
    sessions = sorted(set(recent_sessions))[-5:]
    if any(day > as_of.date() for day in sessions): raise ValueError("future market session is not eligible")
    return {"scope": dict(scope), "target_count": 17758, "price_recheck_sessions": [x.isoformat() for x in sessions],
            "financial_discovery_as_of": as_of.isoformat(), "revision_scan_start": (as_of.date()-timedelta(days=90)).isoformat(),
            "long_revision_scan_partition": as_of.date().toordinal() % 365,
            "calendar_finalization_required": True, "coverage_complete": False}
