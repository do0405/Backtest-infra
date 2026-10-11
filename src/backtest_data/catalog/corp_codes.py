import hashlib
import io
import xml.etree.ElementTree as ET
import zipfile

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ..db import new_id, transaction
from ..ingestion.jobs import stable_hash
from .models import CatalogObservation, Issuer, Security
from ..ingestion.models import TargetUniverseManifest


def parse_corp_codes(content):
    if len(content) > 16 * 1024 * 1024:
        raise ValueError("Corporate directory ZIP too large")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.infolist()
        if len(names) != 1 or names[0].filename.upper() != "CORPCODE.XML" or names[0].file_size > 128 * 1024 * 1024:
            raise ValueError("Corporate directory ZIP schema invalid")
        xml = archive.read(names[0])
    if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise ValueError("XML entities not permitted")
    root = ET.fromstring(xml)
    rows = []
    for item in root.findall("list"):
        row = {node.tag: (node.text or "").strip() for node in item}
        if len(row.get("corp_code", "")) != 8 or not row["corp_code"].isdigit():
            raise ValueError("Invalid corporation identifier")
        rows.append(row)
    if not rows:
        raise ValueError("Empty corporate directory")
    return rows


def sync_corp_codes(engine, dart):
    response = dart.fetch("corpCode.xml", json_response=False)
    rows = parse_corp_codes(response.content)
    digest = hashlib.sha256(response.content).hexdigest()
    with transaction(engine) as session:
        issuers, observations = [], []
        for row in rows:
            issuers.append({"id": new_id(), "market": "KR", "source_identifier": "DART:" + row["corp_code"], "name": row["corp_name"]})
            observations.append({"id": new_id(), "source": "opendart_corp_codes", "identifier": row["corp_code"],
                "content_hash": stable_hash(row), "fields": row, "captured_at": response.captured_at})
        for offset in range(0, len(rows), 500):
            session.execute(insert(Issuer).values(issuers[offset:offset + 500])
                .on_conflict_do_nothing(index_elements=[Issuer.market, Issuer.source_identifier]))
            session.execute(insert(CatalogObservation).values(observations[offset:offset + 500])
                .on_conflict_do_nothing(index_elements=[CatalogObservation.source, CatalogObservation.identifier, CatalogObservation.content_hash]))
        # This directory proves only its explicit primary stock-code mappings.
        # Preferred classes and products are never joined by name similarity.
        issuer_map = {item.source_identifier: item.id for item in session.scalars(select(Issuer).where(Issuer.market == "KR"))}
        manifests = session.scalars(select(TargetUniverseManifest)).all()
        explicit_stock_ids = {}
        for manifest in manifests:
            for member in manifest.members:
                if member["market"] == "KR":
                    explicit_stock_ids.setdefault(member["symbol"], set()).add(member["security_id"])
        mapped = 0
        for row in rows:
            stock_code = row.get("stock_code", "")
            if stock_code and len(explicit_stock_ids.get(stock_code, ())) == 1:
                security_id = next(iter(explicit_stock_ids[stock_code]))
                security = session.get(Security, security_id)
                if security.issuer_id is None:
                    security.issuer_id = issuer_map["DART:" + row["corp_code"]]
                    mapped += 1
    return {"source": "opendart", "corporations": len(rows), "explicit_primary_mappings": mapped, "content_hash": digest,
            "document_archived": False, "historical_identity_verified": False}
