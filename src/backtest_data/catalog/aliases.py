"""Append-only, point-in-time security alias assertions."""

from contextlib import contextmanager
from datetime import date, datetime, timezone
import re
import unicodedata
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..db import utcnow
from ..ingestion.jobs import stable_hash
from ..ingestion.models import TargetUniverseManifest
from .models import CatalogObservation, IdentifierHistory, Issuer, Security


ALIAS_POLICY_VERSION = "security-alias-v1"
UNICODE_VERSION = "15.1.0"
SOURCE_ASSERTION_VERSION = "security-alias-assertion-v1"


def alias_policy():
    return {"policy_version": ALIAS_POLICY_VERSION, "unicode_version": UNICODE_VERSION,
            "normalization": ["NFKC", "casefold", "collapse_whitespace"],
            "source_assertion_version": SOURCE_ASSERTION_VERSION,
            "effective_interval": "[valid_from,valid_to)",
            "economic_date_default": "market_local_cutoff_date",
            "market_timezones": {"KR": "Asia/Seoul", "US": "America/New_York"},
            "assertion_order": ["recorded_at", "available_at", "captured_at", "id"],
            "assertion_selection": "latest_visible_before_status_and_effective_range",
            "generated_availability": "max(requested_available_at,captured_at,recorded_at)",
            "fuzzy_matching": False}


def normalize_alias(value):
    if unicodedata.unidata_version != UNICODE_VERSION:
        raise ValueError("Alias policy requires Unicode " + UNICODE_VERSION)
    if not isinstance(value, str):
        raise ValueError("Alias must be a string")
    normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if not normalized:
        raise ValueError("Alias must not be empty")
    return normalized


def _timestamp(value, field):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(field + " must be an aware datetime") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(field + " must be an aware datetime")
    return value.astimezone(timezone.utc)


def _interval(valid_from, valid_to):
    if type(valid_from) is not date or (valid_to is not None and type(valid_to) is not date):
        raise ValueError("Alias validity must use dates")
    if valid_to is not None and valid_to <= valid_from:
        raise ValueError("Alias interval must be nonempty [valid_from,valid_to)")


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _market_date(cutoff, market):
    zone = {"KR": "Asia/Seoul", "US": "America/New_York"}.get(str(market).upper(), "UTC")
    return cutoff.astimezone(ZoneInfo(zone)).date()


def _rank(row):
    return (_timestamp(row.evidence["recorded_at"], "recorded_at"),
            _timestamp(row.available_at, "available_at"),
            _timestamp(row.captured_at, "captured_at"), str(row.id))


def resolve_records(value, records, securities, *, as_of, history_mode,
                    session_date=None, market=None, venue=None, namespace=None):
    """Resolve only explicit assertions, from live ORM or frozen detached rows.

    Correction selection precedes matching so a withdrawal or changed identifier
    cannot resurrect an older assertion. Catalog names are display metadata only.
    """
    cutoff = _timestamp(as_of, "as_of")
    if history_mode not in {"observed_system", "reconstructed_public"}:
        raise ValueError("Unknown alias history_mode")
    if session_date is not None and type(session_date) is not date:
        raise ValueError("session_date must be a date")
    effective = _market_date(cutoff, market) if session_date is None else session_date
    future_bound = _market_date(cutoff, market) if market else max(
        [_market_date(cutoff, _field(security, "market")) for security in securities.values()] or [cutoff.date()])
    if session_date is not None and session_date > future_bound:
        raise ValueError("session_date cannot be in the future of as_of")
    normalized = normalize_alias(value)
    latest = {}
    for row in records:
        _interval(row.valid_from, row.valid_to)
        available = _timestamp(row.available_at, "available_at")
        captured = _timestamp(row.captured_at, "captured_at")
        evidence = row.evidence or {}
        # Legacy identifier rows are not qualified alias assertions.
        if not evidence.get("assertion_key") or not evidence.get("recorded_at"):
            continue
        recorded = _timestamp(evidence["recorded_at"], "recorded_at")
        if available > cutoff or recorded > cutoff or (history_mode == "observed_system" and captured > cutoff):
            continue
        key = evidence["assertion_key"]
        rank = (recorded, available, captured, str(row.id))
        previous = latest.get(key)
        if previous is None or rank > previous[0]:
            latest[key] = (rank, row)
    matches = {}
    for _, row in latest.values():
        evidence = row.evidence
        if (evidence.get("alias_policy_version") != ALIAS_POLICY_VERSION
                or evidence.get("source_assertion_version") != SOURCE_ASSERTION_VERSION
                or evidence.get("status") != "accepted"
                or evidence.get("normalized_key") != normalized
                or normalize_alias(row.identifier) != normalized
                or not evidence.get("source") or not isinstance(evidence.get("provenance"), dict)
                or not evidence.get("namespace")):
            continue
        security = securities.get(row.security_id)
        if security is None:
            continue
        if market is not None and str(_field(security, "market")).casefold() != market.casefold():
            continue
        if venue is not None and row.venue.casefold() != venue.casefold():
            continue
        if namespace is not None and evidence["namespace"].casefold() != namespace.casefold():
            continue
        local_date = _market_date(cutoff, _field(security, "market"))
        row_date = local_date if session_date is None else session_date
        if row_date > local_date:
            raise ValueError("session_date cannot be in the future of as_of in the security market")
        if row.valid_from > row_date or (row.valid_to is not None and row_date >= row.valid_to):
            continue
        candidate = matches.setdefault(row.security_id, {
            "security_id": row.security_id, "issuer_id": _field(security, "issuer_id"),
            "name": _field(security, "name"), "market": _field(security, "market"),
            "matched_identifiers": [], "session_date": row_date.isoformat(),
        })
        candidate["matched_identifiers"].append({
            "id": row.id, "identifier": row.identifier, "identifier_type": row.identifier_type,
            "namespace": evidence["namespace"], "venue": row.venue,
            "source": evidence["source"], "provenance": evidence["provenance"],
            "assertion_key": evidence["assertion_key"],
            "valid_from": row.valid_from.isoformat(),
            "valid_to": row.valid_to.isoformat() if row.valid_to else None,
            "available_at": _timestamp(row.available_at, "available_at").isoformat(),
            "captured_at": _timestamp(row.captured_at, "captured_at").isoformat(),
            "recorded_at": evidence["recorded_at"],
        })
    candidates = [matches[key] for key in sorted(matches)]
    for candidate in candidates:
        candidate["matched_identifiers"].sort(key=lambda item: str(item["id"]))
    result = {"status": "resolved" if len(candidates) == 1 else "ambiguous" if candidates else "unresolved",
              "candidates": candidates, "policy_version": ALIAS_POLICY_VERSION,
              "normalized_key": normalized, "as_of": cutoff.isoformat(), "history_mode": history_mode,
              "session_date": effective.isoformat(), "historical_identity_qualified": False,
              "temporal": {"effective_interval": "[valid_from,valid_to)",
                           "recording_cutoff": cutoff.isoformat(),
                           "capture_cutoff_applied": history_mode == "observed_system"}}
    if len(candidates) == 1:
        result["security_id"] = candidates[0]["security_id"]
    return result


def _assertion_values(*, security_id, identifier, identifier_type, venue, valid_from,
                      valid_to=None, available_at, captured_at=None, source, namespace,
                      provenance, status="accepted", assertion_key=None, recorded_at=None):
    normalized = normalize_alias(identifier)
    if len(identifier) > 500:
        raise ValueError("Alias identifier exceeds 500 characters")
    _interval(valid_from, valid_to)
    if status not in {"accepted", "withdrawn"}:
        raise ValueError("Alias status must be accepted or withdrawn")
    for name, val, limit in (("security_id", security_id, 36), ("identifier_type", identifier_type, 40),
                             ("venue", venue, 80), ("source", source, 80), ("namespace", namespace, 80)):
        if not isinstance(val, str) or not val.strip() or len(val) > limit:
            raise ValueError("Invalid alias " + name)
    if not isinstance(provenance, dict) or not provenance:
        raise ValueError("Alias provenance must be a nonempty JSON object")
    requested = _timestamp(available_at, "available_at")
    recording = _timestamp(recorded_at if recorded_at is not None else utcnow(), "recorded_at")
    capture = _timestamp(captured_at, "captured_at") if captured_at is not None else recording
    key = assertion_key or stable_hash([source, security_id, identifier_type, venue, namespace, normalized])
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Invalid assertion_key")
    semantic = {"security_id": security_id, "identifier": identifier, "identifier_type": identifier_type,
                "venue": venue, "valid_from": valid_from.isoformat(),
                "valid_to": valid_to.isoformat() if valid_to else None,
                "requested_available_at": requested.isoformat(),
                "source_captured_at": capture.isoformat() if captured_at is not None else None,
                "source": source, "namespace": namespace, "provenance": provenance,
                "status": status, "assertion_key": key, "alias_policy_version": ALIAS_POLICY_VERSION,
                "source_assertion_version": SOURCE_ASSERTION_VERSION}
    # Recording is an invocation fact, excluded from semantic replay identity.
    digest = stable_hash(semantic)
    return {"id": str(uuid5(NAMESPACE_URL, "backtest-security-alias:" + digest)),
            "security_id": security_id, "identifier": identifier, "identifier_type": identifier_type,
            "venue": venue, "valid_from": valid_from, "valid_to": valid_to,
            "available_at": max(requested, capture, recording), "captured_at": capture,
            "evidence": {"alias_policy_version": ALIAS_POLICY_VERSION,
                         "source_assertion_version": SOURCE_ASSERTION_VERSION,
                         "normalized_key": normalized, "namespace": namespace,
                         "source": source, "provenance": provenance, "status": status,
                         "assertion_key": key, "assertion_hash": digest,
                         "recorded_at": recording.isoformat(),
                         "requested_available_at": requested.isoformat()}}


def register_alias(session, **options):
    """Append a deterministic assertion; a concurrent semantic replay is harmless."""
    values = _assertion_values(**options)
    previous_rows = session.scalars(select(IdentifierHistory).where(
        IdentifierHistory.evidence["assertion_key"].astext == values["evidence"]["assertion_key"])).all()
    previous = max((row for row in previous_rows if row.evidence.get("recorded_at")), key=_rank, default=None)
    if previous and previous.evidence.get("assertion_hash") == values["evidence"]["assertion_hash"]:
        return previous
    _chain(values, previous)
    session.execute(insert(IdentifierHistory).values(values).on_conflict_do_nothing(index_elements=[IdentifierHistory.id]))
    return session.get(IdentifierHistory, values["id"])


def _chain(values, previous):
    if previous is not None:
        values["evidence"]["previous_assertion_id"] = previous.id
        values["id"] = str(uuid5(NAMESPACE_URL, "backtest-security-alias:" +
                                 values["evidence"]["assertion_hash"] + ":" + previous.id))


@contextmanager
def _session(database, *, write=False):
    if isinstance(database, Session):
        yield database
    else:
        with Session(database) as session:
            if write:
                with session.begin():
                    yield session
            else:
                yield session


def _manifest(session, manifest_id):
    manifest = session.get(TargetUniverseManifest, manifest_id) if manifest_id else session.scalar(
        select(TargetUniverseManifest).order_by(TargetUniverseManifest.captured_at.desc(), TargetUniverseManifest.id.desc()).limit(1))
    if manifest is None:
        raise ValueError("Target universe manifest not found")
    return manifest


def _directory_evidence(manifest, member):
    expected = member["venue"] if member["market"] == "KR" else (
        "nasdaq" if member["venue"] == "NASDAQ" else "other")
    evidence = [item for item in manifest.scope.get("directory_evidence", [])
                if item.get("market") == member["market"] and item.get("directory") == expected]
    captured = max((_timestamp(item["captured_at"], "directory captured_at") for item in evidence
                    if item.get("captured_at")), default=_timestamp(manifest.captured_at, "manifest captured_at"))
    return captured, evidence


def seed_aliases(database, *, manifest_id=None, market="both"):
    """Register every target member from exact directory and observation evidence.

    Reads and writes are batched; neither issuer identity nor mutable catalog names
    are used to spread aliases across share classes.
    """
    selected_market = market.upper()
    if selected_market not in {"BOTH", "KR", "US"}:
        raise ValueError("Unknown alias market")
    with _session(database, write=True) as session:
        manifest = _manifest(session, manifest_id)
        members = [member for member in manifest.members
                   if selected_market == "BOTH" or member["market"] == selected_market]
        ids = {member["security_id"] for member in members}
        known = {row.id: row for row in session.scalars(select(Security).where(Security.id.in_(ids)))}
        if set(known) != ids:
            raise ValueError("Target contains missing security identities")
        issuer_ids = {row.issuer_id for row in known.values() if row.issuer_id is not None}
        issuers = {row.id: row for row in session.scalars(select(Issuer).where(Issuer.id.in_(issuer_ids)))}
        qualification_gaps = []
        recording = utcnow()
        values_by_id = {}
        target_by_symbol = {}
        common_members = []
        for member in members:
            target_by_symbol.setdefault((member["market"], member["symbol"]), []).append(member)
            if member["market"] == "KR" and member["identifier"] == "KR7005930003":
                common_members.append(member)

        def add(member, identifier, identifier_type, source, namespace, provenance, capture,
                assertion_key=None):
            if not identifier:
                return
            values = _assertion_values(security_id=member["security_id"], identifier=identifier,
                identifier_type=identifier_type, venue=member["venue"],
                valid_from=_market_date(capture, member["market"]), available_at=capture,
                captured_at=capture, recorded_at=recording, source=source,
                namespace=namespace, provenance=provenance, assertion_key=assertion_key)
            values_by_id[values["id"]] = values

        for member in members:
            capture, directory = _directory_evidence(manifest, member)
            provider_symbols = {key: member.get("evidence", {}).get(key) for key in
                                ("ACT Symbol", "CQS Symbol", "NASDAQ Symbol")
                                if member.get("evidence", {}).get(key)}
            provenance = {"manifest_id": manifest.id, "source_identifier": member["identifier"],
                          "directory_evidence": directory, "member_evidence": member.get("evidence", {}),
                          "provider_symbols": provider_symbols,
                          "historical_identity_qualified": False}
            add(member, member["symbol"], "ticker", "target_directory", "universal",
                {**provenance, "field": "symbol"}, capture)
            add(member, member["name"], "security_name", "target_directory", "universal",
                {**provenance, "field": "name"}, capture)
            add(member, member["identifier"], "source_id", "target_directory", "source_id",
                {**provenance, "field": "source_identifier"}, capture)
            if member["market"] == "KR":
                if re.fullmatch(r"KR[A-Z0-9]{10}", member["identifier"]):
                    add(member, member["identifier"], "isin", "target_directory", "isin",
                        {**provenance, "field": "identifier", "format": "KR_ISIN"}, capture)
                if re.fullmatch(r"[0-9]{6}", member["symbol"]):
                    add(member, "A" + member["symbol"], "provider_code", "target_directory", "kr_a_code",
                        {**provenance, "field": "symbol", "format": "A_plus_six_digits"}, capture)
            else:
                for field, namespace in (("ACT Symbol", "act_symbol"), ("CQS Symbol", "cqs_symbol"),
                                         ("NASDAQ Symbol", "nasdaq_symbol")):
                    symbol = provider_symbols.get(field)
                    if symbol and symbol != member["symbol"]:
                        add(member, symbol, "provider_code", "target_directory", namespace,
                            {**provenance, "field": field}, capture)

        observations = session.scalars(select(CatalogObservation).where(
            CatalogObservation.source.in_(["opendart_corp_codes", "sec_current_tickers"]))).all()
        latest_observations = {}
        for observation in observations:
            key = (observation.source, observation.identifier)
            previous = latest_observations.get(key)
            if previous is None or (observation.captured_at, observation.id) > (previous.captured_at, previous.id):
                latest_observations[key] = observation
        identities_by_symbol = {}
        for observation in latest_observations.values():
            source_market = "KR" if observation.source == "opendart_corp_codes" else "US"
            symbol = observation.fields.get("stock_code" if source_market == "KR" else "ticker")
            if symbol:
                identities_by_symbol.setdefault((observation.source, symbol), set()).add(_observation_issuer(observation))
        for observation in latest_observations.values():
            fields = observation.fields
            if observation.source == "opendart_corp_codes":
                targets = target_by_symbol.get(("KR", fields.get("stock_code")), [])
                # DART's primary stock_code proves one exact target security only.
                if len({member["security_id"] for member in targets}) != 1:
                    continue
                name_fields = ("corp_name", "corp_eng_name")
            else:
                targets = target_by_symbol.get(("US", fields.get("ticker")), [])
                name_fields = ("name",)
            for member in targets:
                identity = _observation_issuer(observation)
                current_issuer = issuers.get(known[member["security_id"]].issuer_id)
                reason = None
                symbol = fields.get("stock_code" if member["market"] == "KR" else "ticker")
                if len(identities_by_symbol.get((observation.source, symbol), ())) != 1:
                    reason = "multiple_source_issuers_for_exact_code"
                elif identity is None:
                    reason = "source_issuer_identity_unavailable"
                elif current_issuer is not None and current_issuer.source_identifier != identity:
                    reason = "source_issuer_does_not_match_current_security_issuer"
                if reason:
                    qualification_gaps.append({"source": observation.source, "observation_id": observation.id,
                                               "security_id": member["security_id"], "reason": reason})
                    continue
                for field in name_fields:
                    add(member, fields.get(field), "company_name", observation.source, "universal",
                        {"observation_id": observation.id, "source_identifier": observation.identifier,
                         "content_hash": observation.content_hash, "field": field,
                         "source_issuer_identifier": identity,
                         "current_issuer_mapping_qualified": current_issuer is not None,
                         "explicit_stock_code": fields.get("stock_code"), "explicit_ticker": fields.get("ticker"),
                         "historical_identity_qualified": False}, observation.captured_at,
                        assertion_key=stable_hash([observation.source, observation.identifier,
                                                   field, member["security_id"]]))

        if len({member["security_id"] for member in common_members}) == 1:
            member = common_members[0]
            capture, _ = _directory_evidence(manifest, member)
            add(member, "Samsung Electronics", "company_name", "user_curated_samsung", "universal",
                {"anchor_isin": "KR7005930003",
                 "url": "https://www.samsung.com/global/ir/stock-information/stock-listing/",
                 "curation": "official_ir_name", "historical_identity_qualified": False}, capture)

        previous_rows = session.scalars(select(IdentifierHistory).where(IdentifierHistory.security_id.in_(ids))).all()
        previous_by_key = {}
        for row in previous_rows:
            if row.evidence.get("assertion_key") and row.evidence.get("recorded_at"):
                key = row.evidence["assertion_key"]
                if key not in previous_by_key or _rank(row) > _rank(previous_by_key[key]):
                    previous_by_key[key] = row
        pending = []
        for values in values_by_id.values():
            previous = previous_by_key.get(values["evidence"]["assertion_key"])
            if previous and previous.evidence.get("assertion_hash") == values["evidence"]["assertion_hash"]:
                continue
            _chain(values, previous)
            pending.append(values)
        qualified_source_keys = {values["evidence"]["assertion_key"] for values in values_by_id.values()
                                 if values["identifier_type"] == "company_name" and values["evidence"]["source"] in
                                 {"opendart_corp_codes", "sec_current_tickers"}}
        current_curated_keys = {values['evidence']['assertion_key'] for values in values_by_id.values()
                                if values['evidence']['source']=='user_curated_samsung'}
        withdrawal_ids = set()
        gaps_by_observation = {}
        for gap in qualification_gaps:
            gaps_by_observation.setdefault((gap["security_id"], gap["source"], gap["observation_id"]), []).append(gap)
        for key, previous in previous_by_key.items():
            evidence = previous.evidence
            provenance = evidence.get("provenance", {})
            if (key not in current_curated_keys and evidence.get('status')=='accepted'
                    and previous.identifier_type=='company_name'
                    and evidence.get('source')=='user_curated_samsung'
                    and provenance.get('anchor_isin')=='KR7005930003'
                    and provenance.get('curation') in {'official_ir_name','explicit_user_typo'}
                    and known[previous.security_id].source_identifier=='KR7005930003'
                    and evidence.get('alias_policy_version')==ALIAS_POLICY_VERSION
                    and evidence.get('source_assertion_version')==SOURCE_ASSERTION_VERSION):
                values = _assertion_values(security_id=previous.security_id,
                    identifier=previous.identifier, identifier_type=previous.identifier_type, venue=previous.venue,
                    valid_from=previous.valid_from, valid_to=previous.valid_to,
                    available_at=evidence.get('requested_available_at',previous.captured_at),
                    captured_at=previous.captured_at, recorded_at=recording,
                    source=evidence['source'], namespace=evidence['namespace'], assertion_key=key,
                    status='withdrawn', provenance={**provenance,'withdrawal':{'reason':'curated_alias_removed_by_user'}})
                _chain(values,previous)
                pending.append(values)
                withdrawal_ids.add(values['id'])
                continue
            # Only generated company-name assertions from these exact source
            # observations participate. Directory, curated, and manual aliases
            # retain their own independent evidence and assertion histories.
            if (key in qualified_source_keys or evidence.get("status") != "accepted"
                    or previous.identifier_type != "company_name"
                    or evidence.get("source") not in {"opendart_corp_codes", "sec_current_tickers"}
                    or evidence.get("alias_policy_version") != ALIAS_POLICY_VERSION
                    or evidence.get("source_assertion_version") != SOURCE_ASSERTION_VERSION
                    or not provenance.get("observation_id") or not provenance.get("source_identifier")):
                continue
            observation = latest_observations.get((evidence["source"], provenance["source_identifier"]))
            current_issuer = issuers.get(known[previous.security_id].issuer_id)
            gaps = gaps_by_observation.get((previous.security_id, evidence["source"],
                                            observation.id if observation is not None else None), [])
            values = _assertion_values(security_id=previous.security_id,
                identifier=previous.identifier, identifier_type=previous.identifier_type, venue=previous.venue,
                valid_from=previous.valid_from, valid_to=previous.valid_to,
                available_at=evidence.get("requested_available_at", previous.captured_at),
                captured_at=previous.captured_at, recorded_at=recording,
                source=evidence["source"], namespace=evidence["namespace"], assertion_key=key,
                status="withdrawn", provenance={**provenance, "withdrawal": {
                    "reason": "source_observation_no_longer_qualifies_target_name",
                    "latest_observation_id": observation.id if observation is not None else None,
                    "latest_observation_hash": observation.content_hash if observation is not None else None,
                    "current_issuer_identifier": current_issuer.source_identifier if current_issuer is not None else None,
                    "qualification_gaps": gaps,
                }})
            _chain(values, previous)
            pending.append(values)
            withdrawal_ids.add(values["id"])
        inserted_ids = set()
        for offset in range(0, len(pending), 500):
            inserted_ids.update(session.scalars(insert(IdentifierHistory).values(pending[offset:offset + 500])
                .on_conflict_do_nothing(index_elements=[IdentifierHistory.id]).returning(IdentifierHistory.id)).all())
        session.flush()
        coverage_ids = {values["security_id"] for values in values_by_id.values()}
        return {"status": "research_partial", "manifest_id": manifest.id, "target_count": len(ids),
                "aliases_inserted": len(inserted_ids), "alias_security_count": len(coverage_ids),
                "aliases_withdrawn": len(inserted_ids & withdrawal_ids),
                "coverage": {"target_count": len(ids), "alias_security_count": len(coverage_ids),
                             "missing_security_ids": sorted(ids - coverage_ids)},
                "source_qualification_gaps": qualification_gaps,
                "policy_version": ALIAS_POLICY_VERSION, "historical_identity_qualified": False}


def _observation_issuer(observation):
    if observation.source == "opendart_corp_codes":
        code = str(observation.fields.get("corp_code") or observation.identifier)
        return "DART:" + code if re.fullmatch(r"[0-9]{8}", code) else None
    code = str(observation.fields.get("cik") or observation.identifier.partition(":")[0])
    return "CIK" + code.zfill(10) if re.fullmatch(r"[0-9]{1,10}", code) else None


def resolve_alias(database, value, *, manifest_id=None, **options):
    """Resolve exclusively among the full specified or latest target universe."""
    with _session(database) as session:
        manifest = _manifest(session, manifest_id)
        ids = {member["security_id"] for member in manifest.members}
        securities = {row.id: row for row in session.scalars(select(Security).where(Security.id.in_(ids)))}
        # Load every assertion for each target so changed keys and withdrawals can
        # supersede an older spelling before the resolver applies alias matching.
        assertion_keys = select(IdentifierHistory.evidence["assertion_key"].astext).where(
            IdentifierHistory.security_id.in_(ids))
        rows = session.scalars(select(IdentifierHistory).where(or_(
            IdentifierHistory.security_id.in_(ids),
            IdentifierHistory.evidence["assertion_key"].astext.in_(assertion_keys)))).all()
        result = resolve_records(value, rows, securities, **options)
        result["manifest_id"] = manifest.id
        result["target_count"] = len(ids)
        return result
