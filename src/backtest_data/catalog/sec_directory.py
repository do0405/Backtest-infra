import hashlib
import json

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ..db import new_id, transaction
from ..ingestion.jobs import stable_hash
from ..ingestion.models import TargetUniverseManifest
from .models import CatalogObservation, Issuer, Security


def parse_sec_directory(content):
    payload = json.loads(content)
    if payload.get("fields") != ["cik", "name", "ticker", "exchange"]:
        raise ValueError("SEC directory schema drift")
    rows = []
    for values in payload.get("data", []):
        if len(values) != 4:
            raise ValueError("SEC directory row invalid")
        row = dict(zip(payload["fields"], values, strict=True))
        cik = str(row["cik"])
        if not cik.isdigit() or len(cik) > 10:
            raise ValueError("SEC CIK invalid")
        row["cik"] = cik.zfill(10)
        rows.append(row)
    if not rows:
        raise ValueError("Empty SEC directory")
    return rows


def sync_sec_directory(engine, sec):
    response = sec.fetch("https://www.sec.gov/files/company_tickers_exchange.json")
    rows = parse_sec_directory(response.content)
    issuers = {row["cik"]: {"id": new_id(), "market": "US", "source_identifier": "CIK" + row["cik"], "name": row["name"]} for row in rows}
    observations = [{"id": new_id(), "source": "sec_current_tickers", "identifier": row["cik"] + ":" + row["ticker"],
        "content_hash": stable_hash(row), "fields": row, "captured_at": response.captured_at} for row in rows]
    with transaction(engine) as session:
        values = list(issuers.values())
        for offset in range(0, len(values), 500):
            session.execute(insert(Issuer).values(values[offset:offset + 500])
                            .on_conflict_do_nothing(index_elements=[Issuer.market, Issuer.source_identifier]))
        for offset in range(0, len(observations), 500):
            session.execute(insert(CatalogObservation).values(observations[offset:offset + 500])
                            .on_conflict_do_nothing(index_elements=[CatalogObservation.source, CatalogObservation.identifier, CatalogObservation.content_hash]))
        known = {item.source_identifier: item.id for item in session.scalars(select(Issuer).where(Issuer.market == "US"))}
        target = {}
        for manifest in session.scalars(select(TargetUniverseManifest)):
            for member in manifest.members:
                if member["market"] == "US":
                    target.setdefault(member["symbol"], set()).add(member["security_id"])
        mapped = 0
        ticker_ciks = {}
        for row in rows:
            ticker_ciks.setdefault(row["ticker"], set()).add(row["cik"])
        for ticker, ciks in ticker_ciks.items():
            if len(ciks) != 1 or len(target.get(ticker, ())) != 1:
                continue
            security = session.get(Security, next(iter(target[ticker])))
            if security.issuer_id is None:
                security.issuer_id = known["CIK" + next(iter(ciks))]
                mapped += 1
    return {"source": "sec", "issuer_count": len(issuers), "rows": len(rows), "explicit_current_mappings": mapped,
            "content_hash": hashlib.sha256(response.content).hexdigest(), "historical_identity_verified": False}
