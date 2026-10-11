"""Exact, capture-only KIS US master mappings and full-target price work."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
import io
import re
import zipfile

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ..db import new_id, transaction
from ..ingestion.http import ProviderError
from ..ingestion.jobs import logical_request, stable_hash
from ..ingestion.models import CollectionJob, RawObject, TargetUniverseManifest
from ..ingestion.raw_store import RawStore
from .models import CatalogObservation, Security


MASTER_DIRECTORIES = tuple((exchange,
    f"https://new.real.download.dws.co.kr/common/master/{exchange.lower()}mst.cod.zip")
    for exchange in ("NAS", "NYS", "AMS"))
MASTER_SOURCE = "kis_overseas_master"
MASTER_COLUMNS = ("country", "exchange_id", "exchange", "exchange_name_ko", "provider_symbol",
    "master_realtime_symbol", "name_ko", "name_en", "security_type_code", "currency", "price_precision",
    "data_type", "base_price", "bid_quantity", "ask_quantity", "session_start", "session_end",
    "depositary_receipt_flag", "depositary_receipt_country", "industry_code", "index_membership_flag",
    "tick_type", "product_code", "tick_details")
MAX_MASTER_BYTES = 32 * 1024 * 1024
EXCHANGE_IDS = {"NAS": "22", "NYS": "21", "AMS": "23"}


@dataclass(frozen=True)
class KISOverseasMasterRow:
    provider_symbol: str
    exchange: str
    master_realtime_symbol: str
    fields: dict

    @property
    def realtime_symbol(self):
        # The dailyprice API's response identity prepends D to the official
        # master realtime symbol (verified response: NASAAPL -> DNASAAPL).
        return "D" + self.master_realtime_symbol


def parse_kis_overseas_master(content: bytes, exchange: str) -> list[KISOverseasMasterRow]:
    """Parse the official CP949, 24-column single-member US master ZIP."""
    if exchange not in EXCHANGE_IDS or len(content) > MAX_MASTER_BYTES:
        raise ValueError("Unverified or oversized KIS US master")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if (len(members) != 1 or members[0].filename != exchange + "MST.COD"
                    or members[0].file_size > MAX_MASTER_BYTES or members[0].flag_bits & 1
                    or members[0].is_dir()):
                raise ValueError("Unsafe or changed KIS US master ZIP schema")
            text = archive.read(members[0]).decode("cp949")
    except (zipfile.BadZipFile, UnicodeError, RuntimeError, OSError) as exc:
        raise ValueError("Invalid KIS US master archive") from exc
    result, symbols = [], set()
    for line in text.splitlines():
        values = line.split("\t")
        if len(values) != len(MASTER_COLUMNS):
            raise ValueError("KIS US master column schema drift")
        fields = dict(zip(MASTER_COLUMNS, values, strict=True))
        symbol = fields["provider_symbol"]
        if (fields["country"] != "US" or fields["exchange"] != exchange
                or fields["exchange_id"] != EXCHANGE_IDS[exchange]
                or fields["currency"] != "USD"
                or not re.fullmatch(r"[A-Z0-9][A-Z0-9./^$-]{0,119}", symbol)
                or fields["master_realtime_symbol"] != exchange + symbol
                or not fields["name_en"] or fields["security_type_code"] not in {"1", "2", "3", "4"}
                or not re.fullmatch(r"[0-8]", fields["price_precision"])
                or symbol in symbols):
            raise ValueError("KIS US master identity schema drift")
        symbols.add(symbol)
        result.append(KISOverseasMasterRow(symbol, exchange, fields["master_realtime_symbol"], fields))
    if not result:
        raise ValueError("Empty KIS US master cannot certify a mapping")
    return result


def resolve_kis_overseas_mappings(members, captures):
    """Resolve exact symbols only; a missing master cannot prove uniqueness.

    Capture entries contain exchange, parsed rows, hash, raw_id, captured_at,
    and url. No target exchange or punctuation is translated into a provider
    identity. The result includes each US target, including unresolved ones.
    """
    targets = [member for member in members if member["market"] == "US"]
    target_counts = Counter(member["symbol"] for member in targets)
    candidates = defaultdict(list)
    for capture in captures:
        for row in capture["rows"]:
            if row.exchange != capture["exchange"]:
                raise ValueError("Master capture exchange mismatch")
            candidates[row.provider_symbol].append((row, capture))
    complete = Counter(capture["exchange"] for capture in captures) == Counter(EXCHANGE_IDS.keys())
    result = {}
    for member in targets:
        matched = candidates[member["symbol"]]
        reason = ("kis_overseas_master_incomplete" if not complete else
            "kis_overseas_mapping_ambiguous" if len(matched) > 1 or target_counts[member["symbol"]] > 1 else
            "kis_overseas_mapping_unavailable" if not matched else None)
        decision = {"reason": reason, "candidate_exchanges": sorted({row.exchange for row, _ in matched})}
        if reason is None:
            row, capture = matched[0]
            decision["mapping_evidence"] = {"market": "US", "security_id": member["security_id"],
                "source": MASTER_SOURCE, "provider_symbol": row.provider_symbol, "exchange": row.exchange,
                "qualified_current_mapping": True, "master_hash": capture["hash"],
                "master_raw_id": capture["raw_id"], "master_captured_at": capture["captured_at"],
                "master_url": capture["url"], "master_realtime_symbol": row.master_realtime_symbol,
                "realtime_symbol": row.realtime_symbol, "realtime_symbol_derivation": "D + master_realtime_symbol",
                "match_rule": "exact_symbol_unique_across_complete_us_masters",
                "historical_identity_verified": False}
        result[member["security_id"]] = decision
    return result


def _batches(values, size=500):
    values = list(values)
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def _capture_timestamp(value):
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("Master captured_at requires a timezone-aware timestamp")
    return stamp.astimezone(timezone.utc)


def _validated_targets(session, manifest):
    start, end = (date.fromisoformat(manifest.scope[key]) for key in ("output_start", "output_end"))
    if start > end:
        raise ValueError("Invalid target output interval")
    targets = [member for member in manifest.members if member["market"] == "US"]
    if not targets:
        raise ValueError("Target manifest has no US securities")
    ids = [member["security_id"] for member in targets]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate target security identity")
    securities = {}
    for batch in _batches(ids):
        securities.update((item.id, item) for item in session.scalars(select(Security).where(Security.id.in_(batch))))
    for member in targets:
        security = securities.get(member["security_id"])
        if security is None or security.market != "US" or security.source_identifier != member["identifier"]:
            raise ValueError("Target security market or identifier mismatch")
    return targets


def _work_request(request):
    # Capture evidence changes when a master is recaptured, while the exact
    # provider identity, date range, and unsupported reason define the work.
    return logical_request({key: value for key, value in request.items() if key != "mapping_evidence"})


def plan_kis_overseas_jobs(session, manifest, mappings, *, targets=None):
    """Bulk plan every US target without resetting any existing job state."""
    targets = _validated_targets(session, manifest) if targets is None else targets
    existing = set()
    for batch in _batches(member["security_id"] for member in targets):
        for row in session.execute(select(CollectionJob.entity_id, CollectionJob.request).where(
                CollectionJob.source == "kis_overseas", CollectionJob.domain == "prices",
                CollectionJob.entity_id.in_(batch), CollectionJob.status != "superseded")):
            existing.add(stable_hash(["kis_overseas", "prices", row.entity_id, _work_request(row.request)]))
    planned, reused = [], 0
    for member in targets:
        decision = mappings[member["security_id"]]
        proof = decision.get("mapping_evidence")
        if proof is not None:
            if (not isinstance(proof, dict) or proof.get("market") != "US"
                    or proof.get("security_id") != member["security_id"]
                    or proof.get("provider_symbol") != member["symbol"]
                    or proof.get("exchange") not in EXCHANGE_IDS or proof.get("source") != MASTER_SOURCE
                    or proof.get("qualified_current_mapping") is not True
                    or not isinstance(proof.get("master_raw_id"), str) or not proof["master_raw_id"]
                    or not isinstance(proof.get("master_hash"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", proof["master_hash"])
                    or proof.get("master_realtime_symbol") != proof["exchange"] + member["symbol"]
                    or proof.get("realtime_symbol") != "D" + proof["master_realtime_symbol"]):
                raise ValueError("Unverified KIS US mapping evidence")
        request = {"market": "US", "symbol": member["symbol"], "identifier": member["identifier"],
            "domain": "prices", "output_start": manifest.scope["output_start"],
            "output_end": manifest.scope["output_end"], "warmup_required": False,
            "manifest_id": manifest.id, "identity_history_verified": False,
            "mapping_status": "qualified" if proof else "unsupported"}
        if proof:
            request.update(provider_symbol=proof["provider_symbol"], exchange=proof["exchange"], mapping_evidence=proof)
        else:
            request["mapping_reason"] = decision["reason"]
        identity = stable_hash(["kis_overseas", "prices", member["security_id"], _work_request(request)])
        if identity in existing:
            reused += 1
            continue
        existing.add(identity)
        checkpoint = {} if proof else {"reason": decision["reason"], "market": "US",
            "candidate_exchanges": decision["candidate_exchanges"], "mapping_verified": False,
            "historical_identity_verified": False, "history_coverage_qualified": False}
        planned.append({"id": new_id(), "identity_hash": identity, "source": "kis_overseas", "domain": "prices",
            "entity_id": member["security_id"], "request": request, "status": "pending" if proof else "unsupported",
            "checkpoint": checkpoint, "priority": 10})
    inserted = 0
    for batch in _batches(planned):
        inserted += len(session.execute(insert(CollectionJob).values(batch)
            .on_conflict_do_nothing(index_elements=[CollectionJob.identity_hash]).returning(CollectionJob.id)).all())
    session.flush()
    return {"planned_jobs": inserted, "reused_jobs": reused + len(planned) - inserted}


def save_kis_overseas_catalog(session, manifest, captures, *, gaps=None):
    """Append master capture evidence and plan complete US target coverage.

    Raw objects must already be archived. Observations bind the existing
    Security.source_identifier so release catalog snapshots capture them.
    Replaying the same timestamp/content does not multiply capture evidence.
    """
    targets = _validated_targets(session, manifest)
    captures = [{**capture, "captured_at": _capture_timestamp(capture["captured_at"]).isoformat()}
        for capture in captures]
    raw_ids = {capture["raw_id"] for capture in captures}
    raw_hashes = {}
    for batch in _batches(raw_ids):
        raw_hashes.update((identity, digest) for identity, digest in session.execute(
            select(RawObject.id, RawObject.content_hash).where(RawObject.id.in_(batch))))
    for capture in captures:
        if raw_hashes.get(capture["raw_id"]) != capture["hash"]:
            raise ValueError("Master raw reference hash mismatch")
        _capture_timestamp(capture["captured_at"])
    mappings = resolve_kis_overseas_mappings(targets, captures)
    by_symbol = defaultdict(list)
    for member in targets:
        by_symbol[member["symbol"]].append(member)
    observations, qualified_keys = [], {}
    for capture in captures:
        stamp = _capture_timestamp(capture["captured_at"])
        for row in capture["rows"]:
            for member in by_symbol[row.provider_symbol]:
                proof = mappings[member["security_id"]].get("mapping_evidence")
                fields = {**row.fields, "market": "US", "master_raw_id": capture["raw_id"],
                    "master_hash": capture["hash"], "master_url": capture["url"],
                    "master_captured_at": stamp.isoformat(), "master_capture_date": stamp.date().isoformat(),
                    "realtime_symbol": row.realtime_symbol, "realtime_symbol_derivation": "D + master_realtime_symbol",
                    "qualified_current_mapping": bool(proof), "historical_identity_verified": False,
                    "availability_precision": "capture_only", "schema": "kis_us_master_cp949_tsv_24_v1"}
                digest = stable_hash([member["identifier"], fields])
                observations.append({"id": new_id(), "source": MASTER_SOURCE, "identifier": member["identifier"],
                    "content_hash": digest, "fields": fields, "captured_at": stamp})
                if proof:
                    qualified_keys[member["security_id"]] = digest
    observation_ids = {}
    for batch in _batches(observations):
        returned = session.execute(insert(CatalogObservation).values(batch)
            .on_conflict_do_nothing(index_elements=[CatalogObservation.source, CatalogObservation.identifier,
                CatalogObservation.content_hash]).returning(CatalogObservation.content_hash, CatalogObservation.id))
        observation_ids.update((digest, identity) for digest, identity in returned)
    missing = {item["content_hash"] for item in observations} - observation_ids.keys()
    for batch in _batches(missing):
        observation_ids.update((digest, identity) for digest, identity in session.execute(
            select(CatalogObservation.content_hash, CatalogObservation.id)
            .where(CatalogObservation.source == MASTER_SOURCE, CatalogObservation.content_hash.in_(batch))))
    for identity, digest in qualified_keys.items():
        mappings[identity]["mapping_evidence"]["catalog_observation_id"] = observation_ids[digest]
    planning = plan_kis_overseas_jobs(session, manifest, mappings, targets=targets)
    reasons = Counter(value["reason"] for value in mappings.values() if value["reason"])
    mapping_gaps = [{"domain": "prices", "market": "US", "reason": reason, "target_count": count}
        for reason, count in sorted(reasons.items())]
    mapped = sum("mapping_evidence" in value for value in mappings.values())
    evidence = [{key: value for key, value in capture.items() if key != "rows"} |
        {"rows": len(capture["rows"]), "parse_status": "parsed"} for capture in captures]
    return {"source": "kis_overseas", "status": "partial" if mapped < len(targets) or gaps else "planned",
        "manifest_id": manifest.id, "target_count": len(targets), "mapped_count": mapped,
        "unmapped_count": reasons["kis_overseas_mapping_unavailable"] + reasons["kis_overseas_master_incomplete"],
        "ambiguous_count": reasons["kis_overseas_mapping_ambiguous"], "unsupported_count": len(targets) - mapped,
        **planning, "evidence": evidence, "gaps": list(gaps or []) + mapping_gaps,
        "historical_identity_verified": False, "historical_universe_complete": False}


def sync_kis_overseas_catalog(engine, http, raw_root, *, manifest_id=None):
    """Fetch only official master URLs, archive every received master, then plan.

    Uses ProviderHTTP's directory allowlist/TLS/rate budget. Each master failure
    is retained independently; incomplete masters cannot qualify a mapping.
    """
    with transaction(engine) as session:
        manifest = (session.get(TargetUniverseManifest, manifest_id) if manifest_id else session.scalar(
            select(TargetUniverseManifest).order_by(TargetUniverseManifest.captured_at.desc(),
                TargetUniverseManifest.id.desc()).limit(1)))
        if manifest is None:
            raise ValueError("Target manifest not found")
        _validated_targets(session, manifest)
        selected_manifest_id = manifest.id
    captures, evidence, gaps = [], [], []
    for exchange, url in MASTER_DIRECTORIES:
        try:
            response = http.request("directory", "GET", url)
        except ProviderError as exc:
            gaps.append({"domain": "current_mapping", "market": "US", "exchange": exchange, "reason": exc.code})
            continue
        stamp = _capture_timestamp(response.captured_at)
        try:
            stored = RawStore(raw_root).put(response.content)
            with transaction(engine) as session:
                session.execute(insert(RawObject).values(id=new_id(), content_hash=stored.content_hash,
                    storage_key=stored.storage_key, byte_count=stored.byte_count)
                    .on_conflict_do_nothing(index_elements=[RawObject.content_hash]))
                raw_id = session.scalar(select(RawObject.id).where(RawObject.content_hash == stored.content_hash))
        except (ValueError, OSError):
            gaps.append({"domain": "current_mapping", "market": "US", "exchange": exchange,
                "reason": "kis_overseas_master_archive_failed"})
            continue
        metadata = {"exchange": exchange, "url": url, "raw_id": raw_id,
            "hash": stored.content_hash, "captured_at": stamp.isoformat()}
        try:
            if response.status_code != 200:
                raise ValueError("Unexpected master response status")
            rows = parse_kis_overseas_master(response.content, exchange)
        except ValueError:
            evidence.append({**metadata, "rows": 0, "parse_status": "invalid"})
            gaps.append({"domain": "current_mapping", "market": "US", **metadata,
                "reason": "kis_overseas_master_invalid"})
            continue
        captures.append({**metadata, "rows": rows})
        evidence.append({**metadata, "rows": len(rows), "parse_status": "parsed"})
    with transaction(engine) as session:
        manifest = session.get(TargetUniverseManifest, selected_manifest_id)
        result = save_kis_overseas_catalog(session, manifest, captures, gaps=gaps)
    result["evidence"] = evidence
    return result
