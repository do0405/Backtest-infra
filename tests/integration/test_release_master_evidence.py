from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backtest_data.catalog.models import CatalogObservation, Security, SourceContract
from backtest_data.db import new_id
from backtest_data.fundamentals.models import DerivedFact
from backtest_data.ingestion.models import IngestionAttempt, RawObject, TargetUniverseManifest
from backtest_data.ingestion.raw_store import RawStore
from backtest_data.prices.models import PriceObservation
from backtest_data.releases.build import build_release, collect_candidate
from backtest_data.releases.models import DatasetRelease, SelectionDecision, ValidationIssue
from backtest_data.releases.validation import frozen_rows


pytestmark = pytest.mark.integration
STAMP = datetime(2020, 1, 3, tzinfo=timezone.utc)


@pytest.fixture
def target(db_session, tmp_path):
    security = Security(market="US", source_identifier="nasdaq_directory:AAPL", name="Apple", security_type="common_stock")
    db_session.add(security)
    db_session.flush()
    manifest = TargetUniverseManifest(content_hash=new_id().replace("-", "") * 2,
        scope={"markets": ["US"], "output_start": "2020-01-01", "output_end": "2020-12-31",
            "historical_universe_complete": False},
        members=[{"market": "US", "security_id": security.id, "symbol": "AAPL"}], gaps=[])
    db_session.add(manifest)
    db_session.flush()
    return SimpleNamespace(session=db_session, security=security, manifest=manifest, raw_root=tmp_path)


def archived(target, content=b"official master capture"):
    stored = RawStore(target.raw_root).put(content)
    raw = RawObject(content_hash=stored.content_hash, storage_key=stored.storage_key, byte_count=stored.byte_count)
    target.session.add(raw)
    target.session.flush()
    return raw


def observation(target, raw, *, fields=None, security=None):
    value = CatalogObservation(source="kis_overseas_master", identifier=(security or target.security).source_identifier,
        content_hash=new_id().replace("-", "") * 2, captured_at=STAMP,
        fields={"market": "US", "master_raw_id": raw.id, "master_hash": raw.content_hash,
            "provider_symbol": "AAPL", "historical_identity_verified": False} if fields is None else fields)
    target.session.add(value)
    target.session.flush()
    return value


def contract(target):
    value = SourceContract(source="kis_overseas", version=new_id(), domain="prices", market="US",
        specification={"basis": "as_traded"}, qualification="contract_defined", content_hash="a" * 64)
    target.session.add(value)
    target.session.flush()
    return value


def price(target, raw, *, proof=None):
    price_raw = archived(target, b"price response")
    source = contract(target)
    value = PriceObservation(security_id=target.security.id, source_contract_id=source.id, raw_id=price_raw.id,
        session=date(2020, 1, 2), basis="as_traded", feed="kis:us:NAS", venue="NAS", trade_scope="unqualified",
        currency="USD", quantity_unit="shares", open=Decimal("100"), high=Decimal("100"), low=Decimal("100"),
        close=Decimal("100"), volume=Decimal("10"), notional=None, signature=new_id().replace("-", "") * 2,
        available_at=STAMP, captured_at=STAMP, evidence={"availability_precision": "capture_only",
            "historical_identity_verified": False, "current_mapping_evidence": proof if proof is not None else
            {"master_raw_id": raw.id, "master_hash": raw.content_hash}})
    target.session.add(value)
    target.session.flush()
    target.session.add(SelectionDecision(record_domain="price_observation", record_id=value.id, status="accepted",
        rule_version="kis_overseas_raw_price_structure_v1", evidence_grade="contract_verified", reason="Raw capture",
        actor="test", recorded_at=STAMP))
    target.session.flush()
    return value


def seal(target):
    result = build_release(target.session, target.manifest.id, raw_root=target.raw_root)
    release = target.session.get(DatasetRelease, result["release_id"])
    return result, release, frozen_rows(target.session, release)


def test_catalog_only_master_is_sealed_and_keeps_full_unpriced_target(target):
    raw = archived(target)
    capture = observation(target, raw)
    result, release, rows = seal(target)
    assert raw.id in rows["raw_object"]
    assert result["target_count"] == 1 and result["counts"]["price_observation"] == 0
    assert release.manifest["catalog"]["observations"][0]["id"] == capture.id
    assert release.manifest["coverage"]["profiles"]["core_price_volume"] is False
    assert any(gap["reason"] == "prices_unavailable" for gap in result["gaps"])


def test_catalog_master_dependencies_include_attempt_contract_and_issue(target):
    raw = archived(target)
    observation(target, raw)
    source = contract(target)
    attempt = IngestionAttempt(source_contract_id=source.id, request_hash="b" * 64, content_hash=raw.content_hash,
        status="received", captured_at=STAMP, evidence={})
    issue = ValidationIssue(record_domain="raw_object", record_id=raw.id, rule="master_review", severity="gap",
        evidence={"raw_id": raw.id})
    target.session.add_all([attempt, issue])
    target.session.flush()
    _, _, rows = seal(target)
    assert attempt.id in rows["ingestion_attempt"] and source.id in rows["source_contract"]
    assert issue.id in rows["validation_issue"]


def test_repeated_catalog_captures_and_price_proof_share_one_master_archive(target):
    raw = archived(target)
    first, second = observation(target, raw), observation(target, raw)
    value = price(target, raw)
    result, release, rows = seal(target)
    assert set(rows["raw_object"]) == {raw.id, value.raw_id}
    assert result["counts"]["selection_decision"] == 1
    assert {row["id"] for row in release.manifest["catalog"]["observations"]} == {first.id, second.id}
    assert rows["price_observation"][value.id].evidence["historical_identity_verified"] is False


@pytest.mark.parametrize("origin", ["catalog", "price"])
def test_mismatched_declared_master_hash_cannot_seal(target, origin):
    raw = archived(target)
    fields = {"master_raw_id": raw.id, "master_hash": "0" * 64}
    if origin == "catalog":
        observation(target, raw, fields=fields)
    else:
        price(target, raw, proof=fields)
    with pytest.raises(ValueError, match="master_evidence_hash_mismatch"):
        seal(target)
    assert target.session.scalar(select(DatasetRelease)) is None


@pytest.mark.parametrize("changes", [{"master_raw_id": None}, {"master_hash": None},
    {"master_hash": "invalid"}, {"master_hash": "A" * 64}])
@pytest.mark.parametrize("origin", ["catalog", "price"])
def test_malformed_master_reference_is_rejected_for_catalog_and_price(target, changes, origin):
    raw = archived(target)
    fields = {"master_raw_id": raw.id, "master_hash": raw.content_hash, **changes}
    if origin == "catalog":
        observation(target, raw, fields=fields)
    else:
        price(target, raw, proof=fields)
    with pytest.raises(ValueError, match="undefined_master_evidence"):
        seal(target)
    assert target.session.scalar(select(DatasetRelease)) is None


def test_catalog_master_missing_sql_object_or_missing_archive_cannot_seal(target):
    missing = SimpleNamespace(id=new_id(), content_hash="a" * 64)
    observation(target, missing)
    with pytest.raises(ValueError, match="missing_dependency:raw_object"):
        seal(target)


def test_catalog_master_archive_integrity_is_checked_without_any_prices(target):
    raw = archived(target)
    observation(target, raw)
    RawStore(target.raw_root)._path(raw.storage_key).unlink()
    with pytest.raises(ValueError, match="raw_archive_missing"):
        seal(target)


def test_catalog_master_inclusion_obeys_the_full_release_row_budget(target, monkeypatch):
    import backtest_data.releases.build as module
    raw = archived(target)
    observation(target, raw)
    monkeypatch.setattr(module, "MAX_RELEASE_ROWS", 1)
    with pytest.raises(ValueError, match="release_row_budget_exceeded"):
        seal(target)
    assert target.session.scalar(select(DatasetRelease)) is None


def test_dependency_introduced_security_catalog_master_is_also_closed(target):
    outside = Security(market="US", source_identifier="nasdaq_directory:OUTSIDE", name="Outside dependency",
        security_type="common_stock")
    target.session.add(outside)
    target.session.flush()
    master = archived(target)
    observation(target, master, security=outside)
    outside_target = SimpleNamespace(session=target.session, security=outside, raw_root=target.raw_root)
    outside_price = price(outside_target, master)
    root = DerivedFact(entity_id=target.security.id, entity_type="security", metric_id="fixture",
        definition_version="fixture", definition_hash="a" * 64, signature=new_id().replace("-", "") * 2,
        value=None, unit="USD", status="unsupported", reason="fixture", period={}, basis={},
        available_at=STAMP, captured_at=STAMP, materialized_at=STAMP, evaluation_as_of=STAMP,
        input_refs=[{"domain": "price_observation", "id": outside_price.id}])
    target.session.add(root)
    target.session.flush()
    rows, catalog, security_ids, _ = collect_candidate(target.session, target.manifest)
    assert master.id in rows["raw_object"] and outside_price.id in rows["price_observation"]
    assert outside.id in catalog["securities"] and outside.id not in security_ids
    assert catalog["observations"][0]["fields"]["master_raw_id"] == master.id
