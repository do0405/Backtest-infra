from dataclasses import asdict
from uuid import uuid5, NAMESPACE_URL

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ..db import new_id, transaction, utcnow
from ..ingestion.jobs import stable_hash, logical_request
from ..ingestion.models import CollectionJob, TargetUniverseManifest, RawObject
from ..ingestion.raw_store import RawStore
from .models import Security, IdentifierHistory
from .universe import required_work, parse_us_directory, parse_kis_directory


def save_universe(session, entries, *, output_start, output_end, evidence, gaps):
    members = []
    securities, identifiers = [], []
    for item in entries:
        security_id = str(uuid5(NAMESPACE_URL, f"backtest:{item.market}:{item.identifier}"))
        securities.append({"id": security_id, "market": item.market, "source_identifier": item.identifier,
                           "security_type": item.security_type, "name": item.name})
        members.append({**asdict(item), "security_id": security_id})
    # Domain support never removes a security from the target denominator.
    for offset in range(0, len(securities), 500):
        session.execute(insert(Security).values(securities[offset:offset + 500])
                        .on_conflict_do_nothing(index_elements=[Security.market, Security.source_identifier]))
    scope = {"output_start": output_start, "output_end": output_end, "markets": sorted({x.market for x in entries}),
             "directory_evidence": evidence,
             "historical_universe_complete": False, "current_universe_complete": not any(g.get("domain") == "current_universe" for g in gaps)}
    content = {"members": sorted(members, key=lambda x: x["security_id"]), "scope": scope, "gaps": gaps, "evidence": evidence}
    digest = stable_hash(content)
    session.execute(insert(TargetUniverseManifest).values(id=new_id(), content_hash=digest, scope=scope,
        members=content["members"], gaps=gaps).on_conflict_do_nothing(index_elements=[TargetUniverseManifest.content_hash]))
    manifest = session.scalar(select(TargetUniverseManifest).where(TargetUniverseManifest.content_hash == digest))
    by_identifier = {(item["market"], item["identifier"]): item["security_id"] for item in members}
    jobs = []
    existing_work = {stable_hash([row.source, row.domain, row.entity_id, logical_request(row.request)])
                     for row in session.execute(select(CollectionJob.source, CollectionJob.domain, CollectionJob.entity_id, CollectionJob.request)
                        .where(CollectionJob.status != "superseded"))}
    for work in required_work(entries, output_start=output_start, output_end=output_end):
        entity_id = by_identifier[(work["market"], work["identifier"])]
        source = ("kis" if work["market"] == "KR" else "alpaca") if work["domain"] in {"prices", "vwap"} else ("opendart" if work["market"] == "KR" else "sec")
        request = {**work, "manifest_id": manifest.id, "identity_history_verified": False}
        identity_hash = stable_hash([source, work["domain"], entity_id, logical_request(request)])
        if identity_hash in existing_work:
            continue
        existing_work.add(identity_hash)
        jobs.append({"id": new_id(), "identity_hash": identity_hash, "source": source, "domain": work["domain"],
                     "entity_id": entity_id, "request": request})
    for offset in range(0, len(jobs), 500):
        session.execute(insert(CollectionJob).values(jobs[offset:offset + 500])
                        .on_conflict_do_nothing(index_elements=[CollectionJob.identity_hash]))
    session.flush()
    return manifest


DIRECTORIES = (
    ("US", "nasdaq", "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"),
    ("US", "other", "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"),
    ("KR", "KOSPI", "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip"),
    ("KR", "KOSDAQ", "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip"),
    ("KR", "KONEX", "https://new.real.download.dws.co.kr/common/master/konex_code.mst.zip"),
)


def sync_universe(engine, http, raw_root, *, market, output_start, output_end):
    entries, evidence, gaps = [], [], [{"domain": "historical_universe", "reason": "historical_lifecycle_source_not_qualified"}]
    for item_market, name, url in DIRECTORIES:
        if market != "both" and item_market.lower() != market:
            continue
        try:
            response = http.request("directory", "GET", url)
            parsed = parse_us_directory(response.content, name) if item_market == "US" else parse_kis_directory(response.content, name)
            stored = RawStore(raw_root).put(response.content)
            with transaction(engine) as session:
                session.execute(insert(RawObject).values(id=new_id(), content_hash=stored.content_hash,
                    storage_key=stored.storage_key, byte_count=stored.byte_count).on_conflict_do_nothing(index_elements=[RawObject.content_hash]))
                raw_id = session.scalar(select(RawObject.id).where(RawObject.content_hash == stored.content_hash))
            entries.extend(parsed)
            evidence.append({"market": item_market, "directory": name, "url": url, "hash": stored.content_hash,
                             "raw_id": raw_id, "rows": len(parsed), "captured_at": response.captured_at.isoformat()})
        except (ValueError, OSError) as exc:
            gaps.append({"domain": "current_universe", "market": item_market, "directory": name, "reason": type(exc).__name__})
        except Exception as exc:
            from ..ingestion.http import ProviderError
            if not isinstance(exc, ProviderError):
                raise
            gaps.append({"domain": "current_universe", "market": item_market, "directory": name, "reason": exc.code})
    if not entries:
        return {"status": "blocked", "gaps": gaps, "evidence": evidence}
    with transaction(engine) as session:
        manifest = save_universe(session, entries, output_start=output_start, output_end=output_end, evidence=evidence, gaps=gaps)
        result = {"status": "partial", "manifest_id": manifest.id, "target_count": len(manifest.members), "scope": manifest.scope,
                  "gaps": gaps, "evidence": evidence}
    return result
