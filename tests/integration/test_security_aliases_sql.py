from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session
from concurrent.futures import ThreadPoolExecutor

from backtest_data.catalog.aliases import register_alias, resolve_alias, seed_aliases
from backtest_data.catalog.collection import save_universe
from backtest_data.catalog.models import CatalogObservation, IdentifierHistory, Issuer, Security
from backtest_data.catalog.universe import UniverseEntry
from backtest_data.db import new_id


pytestmark = pytest.mark.integration
STAMP = datetime(2024, 1, 1, tzinfo=timezone.utc)


def security(session, code="KR7005930003", name="삼성전자", market="KR"):
    row = Security(id=new_id(), market=market, source_identifier=code,
                   security_type="common", name=name)
    session.add(row)
    session.flush()
    return row


def register(session, row, **options):
    args = dict(security_id=row.id, identifier="005930", identifier_type="ticker", venue="KOSPI",
                valid_from=STAMP.date(), available_at=STAMP, captured_at=STAMP,
                source="fixture", namespace="universal", provenance={"source_id": "fixture"}, recorded_at=STAMP)
    args.update(options)
    return register_alias(session, **args)


def test_registry_replay_ignores_recording_clock_and_assertion_corrections_append(db_session):
    row = security(db_session)
    alias = register(db_session, row, assertion_key="same")
    replay = register(db_session, row, assertion_key="same", recorded_at=STAMP + timedelta(days=1))
    assert replay.id == alias.id
    withdrawn = register(db_session, row, assertion_key="same", status="withdrawn",
                         available_at=STAMP + timedelta(days=2), captured_at=STAMP + timedelta(days=2),
                         recorded_at=STAMP + timedelta(days=2))
    assert withdrawn.id != alias.id
    assert db_session.scalar(select(func.count()).select_from(IdentifierHistory)) == 2
    with pytest.raises(Exception, match="immutable"):
        with db_session.begin_nested():
            db_session.execute(text("UPDATE identifier_history SET identifier='changed' WHERE id=:id"), {"id": alias.id})


def test_new_assertion_availability_cannot_be_backdated_before_recording(db_session):
    row = security(db_session)
    recorded = STAMP + timedelta(days=50)
    alias = register(db_session, row, identifier="Samsung Electronics", identifier_type="security_name", recorded_at=recorded)
    assert alias.available_at == recorded
    assert alias.evidence["recorded_at"] == recorded.isoformat()
    for options in ({"available_at": datetime(2024, 1, 1)}, {"valid_to": STAMP.date()},
                    {"status": "maybe"}, {"valid_from": "2024-01-01"}):
        with pytest.raises(ValueError):
            register(db_session, row, **options)


def test_withdrawn_alias_can_be_reaccepted_and_each_transition_is_idempotent(db_session):
    row = security(db_session)
    accepted = register(db_session, row, assertion_key="reinstatement")
    withdrawn = register(db_session, row, assertion_key="reinstatement", status="withdrawn",
                         recorded_at=STAMP + timedelta(days=2))
    restored = register(db_session, row, assertion_key="reinstatement", recorded_at=STAMP + timedelta(days=3))
    assert len({accepted.id, withdrawn.id, restored.id}) == 3
    assert register(db_session, row, assertion_key="reinstatement", recorded_at=STAMP + timedelta(days=4)).id == restored.id
    from backtest_data.catalog.aliases import resolve_records
    assert resolve_records("005930", [accepted, withdrawn, restored], {row.id: row},
                           as_of=STAMP + timedelta(days=5), history_mode="observed_system")["security_id"] == row.id


def test_later_recording_supersedes_prior_later_availability(db_session):
    row = security(db_session)
    initial = register(db_session, row, assertion_key="late-visible", available_at=STAMP + timedelta(days=9))
    closed = register(db_session, row, assertion_key="late-visible", valid_to=date(2024, 1, 5),
                      recorded_at=STAMP + timedelta(days=3))
    from backtest_data.catalog.aliases import resolve_records
    assert resolve_records("005930", [initial, closed], {row.id: row}, as_of=STAMP + timedelta(days=15),
                           history_mode="observed_system")["status"] == "unresolved"


def test_seed_covers_entire_target_and_separates_common_preferred_and_provider_names(db_session):
    entries = [
        UniverseEntry("KR", "KOSPI", "005930", "KR7005930003", "삼성전자", "common", {}),
        UniverseEntry("KR", "KOSPI", "005935", "KR7005931001", "삼성전자우", "preferred", {}),
        UniverseEntry("US", "NYSE", "BRK.B", "nasdaq_directory:BRK.B", "Class B", "common",
                      {"ACT Symbol": "BRK.B", "CQS Symbol": "BRK-B", "NASDAQ Symbol": "BRK/B"}),
    ] + [UniverseEntry("KR", "KOSDAQ", f"{i:06d}", f"KR{i:010d}", f"Product {i}", "etp_unknown", {}) for i in range(150)]
    manifest = save_universe(db_session, entries, output_start="2016-01-01", output_end="2026-10-07",
                            evidence=[{"market": "KR", "directory": "KOSPI", "captured_at": STAMP.isoformat(), "hash": "master-hash"},
                                      {"market": "KR", "directory": "KOSDAQ", "captured_at": STAMP.isoformat()},
                                      {"market": "US", "directory": "other", "captured_at": STAMP.isoformat()}], gaps=[])
    db_session.add_all([
        CatalogObservation(id=new_id(), source="opendart_corp_codes", identifier="00126380", content_hash="d" * 64,
                           fields={"stock_code": "005930", "corp_name": "삼성전자", "corp_eng_name": "SAMSUNG ELECTRONICS"}, captured_at=STAMP),
        CatalogObservation(id=new_id(), source="sec_current_tickers", identifier="0001067983:BRK.B", content_hash="s" * 64,
                           fields={"ticker": "BRK.B", "name": "Berkshire Hathaway"}, captured_at=STAMP),
    ])
    db_session.flush()
    result = seed_aliases(db_session, manifest_id=manifest.id)
    assert result["target_count"] == 153
    assert result["alias_security_count"] == 153
    assert result["historical_identity_qualified"] is False
    assert result["aliases_inserted"] > 500
    assert seed_aliases(db_session, manifest_id=manifest.id)["aliases_inserted"] == 0
    as_of = datetime.now(timezone.utc) + timedelta(seconds=5)
    def lookup(value, **options):
        return resolve_alias(db_session, value, manifest_id=manifest.id, as_of=as_of,
                             history_mode="observed_system", **options)
    common = lookup("005930")["security_id"]
    for alias in ("A005930", "삼성전자", "Samsung Electronics", "KR7005930003"):
        assert lookup(alias)["security_id"] == common
    preferred = lookup("005935")["security_id"]
    assert preferred != common
    assert lookup("A005935")["security_id"] == preferred
    assert lookup("BRK-B", namespace="cqs_symbol")["security_id"] == lookup("BRK.B")["security_id"]
    assert lookup("BRK/B", namespace="nasdaq_symbol")["status"] == "resolved"
    assert lookup("000149")["status"] == "resolved"
    assert lookup("005930", session_date=date(2016, 1, 1))["status"] == "unresolved"
    assert resolve_alias(db_session, "005930", manifest_id=manifest.id, as_of=STAMP,
                         history_mode="reconstructed_public")["status"] == "unresolved"
    assert lookup("Samung Electronics")["status"] == "unresolved"
    curated = db_session.scalar(select(IdentifierHistory).where(IdentifierHistory.identifier == "Samsung Electronics"))
    assert curated.evidence["provenance"]["url"] == "https://www.samsung.com/global/ir/stock-information/stock-listing/"


def test_seeding_withdraws_removed_curated_typo_and_never_recreates_it(db_session):
    manifest = save_universe(db_session,
        [UniverseEntry('KR','KOSPI','005930','KR7005930003','삼성전자','common',{})],
        output_start='2016-01-01',output_end='2026-10-07',evidence=[],gaps=[])
    target = db_session.get(Security, manifest.members[0]['security_id'])
    original = register(db_session, target, identifier='Samung Electronics', identifier_type='company_name',
        source='user_curated_samsung', provenance={'anchor_isin':'KR7005930003',
            'url':'https://www.samsung.com/global/ir/stock-information/stock-listing/',
            'curation':'explicit_user_typo','historical_identity_qualified':False})
    result = seed_aliases(db_session,manifest_id=manifest.id)
    assert result['aliases_withdrawn']==1
    lookup_args = dict(manifest_id=manifest.id,history_mode='observed_system')
    assert resolve_alias(db_session,'Samung Electronics',as_of=datetime.now(timezone.utc),**lookup_args)['status']=='unresolved'
    assert resolve_alias(db_session,'Samsung Electronics',as_of=datetime.now(timezone.utc),**lookup_args)['security_id']==target.id
    assert resolve_alias(db_session,'Samung Electronics',as_of=STAMP+timedelta(days=1),**lookup_args)['security_id']==target.id
    assert original.evidence['status']=='accepted'
    replay = seed_aliases(db_session,manifest_id=manifest.id)
    assert replay['aliases_inserted']==replay['aliases_withdrawn']==0


def test_live_lookup_restricts_to_latest_target_and_long_names_are_stored(db_session):
    old = save_universe(db_session, [UniverseEntry("US", "NYSE", "OLD", "old", "x" * 400, "etf", {})],
                        output_start="2016-01-01", output_end="2026-10-07", evidence=[], gaps=[])
    seed_aliases(db_session, manifest_id=old.id)
    new = save_universe(db_session, [UniverseEntry("US", "NYSE", "NEW", "new", "New ETF", "etf", {})],
                        output_start="2016-01-01", output_end="2026-10-08", evidence=[], gaps=[])
    seed_aliases(db_session, manifest_id=new.id)
    options = dict(as_of=datetime.now(timezone.utc) + timedelta(seconds=5), history_mode="observed_system")
    assert resolve_alias(db_session, "OLD", **options)["status"] == "unresolved"
    assert resolve_alias(db_session, "x" * 400, manifest_id=old.id, **options)["status"] == "resolved"
    indexes = inspect(db_session.connection()).get_indexes("identifier_history")
    assert any(index["name"] == "ix_identifier_history_alias_normalized_key" for index in indexes)
    normalized = next(index for index in indexes if index["name"] == "ix_identifier_history_alias_normalized_key")
    assert "md5" in normalized["expressions"][0]


def test_sec_shared_company_names_remain_ambiguous_across_classes(db_session):
    entries = [UniverseEntry("US", "NYSE", ticker, "directory:" + ticker, ticker + " Class", "common", {})
               for ticker in ("DUAL.A", "DUAL.B")]
    manifest = save_universe(db_session, entries, output_start="2016-01-01", output_end="2026-10-07", evidence=[], gaps=[])
    db_session.add_all([CatalogObservation(id=new_id(), source="sec_current_tickers", identifier="0000000001:" + ticker,
                                          content_hash=ticker.ljust(64, "0"), fields={"ticker": ticker, "name": "Dual Company"},
                                          captured_at=STAMP) for ticker in ("DUAL.A", "DUAL.B")])
    db_session.flush()
    seed_aliases(db_session, manifest_id=manifest.id)
    options = dict(manifest_id=manifest.id, as_of=datetime.now(timezone.utc) + timedelta(seconds=5), history_mode="observed_system")
    result = resolve_alias(db_session, "Dual Company", **options)
    assert result["status"] == "ambiguous"
    assert len(result["candidates"]) == 2
    assert resolve_alias(db_session, "DUAL.A", **options)["security_id"] != resolve_alias(db_session, "DUAL.B", **options)["security_id"]


def test_stale_company_observations_cannot_cross_current_issuer_identity(db_session):
    entries = [UniverseEntry("US", "NYSE", "REUSED", "directory:REUSED", "Current Security", "common", {}),
               UniverseEntry("US", "NYSE", "CONFLICT", "directory:CONFLICT", "Conflicted Security", "common", {}),
               UniverseEntry("KR", "KOSPI", "123456", "KR1234567890", "현재종목", "common", {})]
    manifest = save_universe(db_session, entries, output_start="2016-01-01", output_end="2026-10-07", evidence=[], gaps=[])
    current_issuer = Issuer(id=new_id(), market="US", source_identifier="CIK0000000002", name="New issuer")
    kr_issuer = Issuer(id=new_id(), market="KR", source_identifier="DART:00000002", name="새발행인")
    db_session.add_all([current_issuer, kr_issuer])
    db_session.flush()
    for member in manifest.members:
        row = db_session.get(Security, member["security_id"])
        if member["symbol"] == "REUSED":
            row.issuer_id = current_issuer.id
        if member["symbol"] == "123456":
            row.issuer_id = kr_issuer.id
    db_session.add_all([
        CatalogObservation(id=new_id(), source="sec_current_tickers", identifier="0000000001:REUSED", content_hash="a" * 64,
                           fields={"cik": "0000000001", "ticker": "REUSED", "name": "Old company"}, captured_at=STAMP),
        CatalogObservation(id=new_id(), source="sec_current_tickers", identifier="0000000001:CONFLICT", content_hash="b" * 64,
                           fields={"cik": "0000000001", "ticker": "CONFLICT", "name": "Conflicting company one"}, captured_at=STAMP),
        CatalogObservation(id=new_id(), source="sec_current_tickers", identifier="0000000002:CONFLICT", content_hash="c" * 64,
                           fields={"cik": "0000000002", "ticker": "CONFLICT", "name": "Conflicting company two"}, captured_at=STAMP),
        CatalogObservation(id=new_id(), source="opendart_corp_codes", identifier="00000001", content_hash="d" * 64,
                           fields={"corp_code": "00000001", "stock_code": "123456", "corp_eng_name": "Old Korean company"}, captured_at=STAMP),
    ])
    db_session.flush()
    result = seed_aliases(db_session, manifest_id=manifest.id)
    assert len(result["source_qualification_gaps"]) >= 3
    for name in ("Old company", "Conflicting company one", "Conflicting company two", "Old Korean company"):
        assert resolve_alias(db_session, name, manifest_id=manifest.id, as_of=datetime.now(timezone.utc) + timedelta(seconds=5),
                             history_mode="observed_system")["status"] == "unresolved"


@pytest.mark.parametrize(("market", "symbol", "identifier", "source", "observation_identifier", "fields", "issuer_identifier", "company_name"), [
    ("US", "REUSED", "directory:REUSED", "sec_current_tickers", "0000000001:REUSED",
     {"cik": "0000000001", "ticker": "REUSED", "name": "Previously mapped company"}, "CIK0000000002", "Previously mapped company"),
    ("KR", "123456", "KR1234567890", "opendart_corp_codes", "00000001",
     {"corp_code": "00000001", "stock_code": "123456", "corp_eng_name": "Previously mapped Korean company"},
     "DART:00000002", "Previously mapped Korean company"),
])
def test_later_issuer_conflict_withdraws_previously_seeded_company_alias(db_session, market, symbol, identifier,
                                                                      source, observation_identifier, fields,
                                                                      issuer_identifier, company_name):
    import time
    venue = "NYSE" if market == "US" else "KOSPI"
    manifest = save_universe(db_session, [UniverseEntry(market, venue, symbol, identifier, "Directory security name", "common", {})],
                            output_start="2016-01-01", output_end="2026-10-07", evidence=[], gaps=[])
    db_session.add(CatalogObservation(id=new_id(), source=source, identifier=observation_identifier,
                                     content_hash="f" * 64, fields=fields, captured_at=STAMP))
    db_session.flush()
    seed_aliases(db_session, manifest_id=manifest.id)
    before = datetime.now(timezone.utc)
    options = dict(manifest_id=manifest.id, history_mode="observed_system")
    assert resolve_alias(db_session, company_name, as_of=before, **options)["status"] == "resolved"
    issuer = Issuer(id=new_id(), market=market, source_identifier=issuer_identifier, name="Contradicting current issuer")
    db_session.add(issuer)
    db_session.flush()
    db_session.get(Security, manifest.members[0]["security_id"]).issuer_id = issuer.id
    db_session.flush()
    # Ensure the old cutoff is strictly before the generated withdrawal time.
    time.sleep(0.002)
    result = seed_aliases(db_session, manifest_id=manifest.id)
    assert result["source_qualification_gaps"]
    assert result["aliases_inserted"] == 1
    assert resolve_alias(db_session, company_name, as_of=datetime.now(timezone.utc), **options)["status"] == "unresolved"
    assert resolve_alias(db_session, company_name, as_of=before, **options)["status"] == "resolved"
    assert seed_aliases(db_session, manifest_id=manifest.id)["aliases_inserted"] == 0
    matching_identifier = "CIK0000000001" if market == "US" else "DART:00000001"
    matching_issuer = Issuer(id=new_id(), market=market, source_identifier=matching_identifier, name="Matching source issuer")
    db_session.add(matching_issuer)
    db_session.flush()
    db_session.get(Security, manifest.members[0]["security_id"]).issuer_id = matching_issuer.id
    db_session.flush()
    assert seed_aliases(db_session, manifest_id=manifest.id)["aliases_inserted"] == 1
    assert resolve_alias(db_session, company_name, as_of=datetime.now(timezone.utc), **options)["status"] == "resolved"
    assert seed_aliases(db_session, manifest_id=manifest.id)["aliases_inserted"] == 0


@pytest.mark.parametrize("changed_fields", [{"corp_code": "00000001", "stock_code": "654321", "corp_eng_name": "Old primary company"},
                                          {"corp_code": "00000001", "stock_code": "123456"}])
def test_changed_primary_code_or_removed_name_withdraws_old_observation_alias(db_session, changed_fields):
    manifest = save_universe(db_session, [UniverseEntry("KR", "KOSPI", "123456", "KR1234567890", "Directory security", "common", {})],
                            output_start="2016-01-01", output_end="2026-10-07", evidence=[], gaps=[])
    db_session.add(CatalogObservation(id=new_id(), source="opendart_corp_codes", identifier="00000001", content_hash="a" * 64,
        fields={"corp_code": "00000001", "stock_code": "123456", "corp_eng_name": "Old primary company"}, captured_at=STAMP))
    db_session.flush()
    seed_aliases(db_session, manifest_id=manifest.id)
    before = datetime.now(timezone.utc)
    options = dict(manifest_id=manifest.id, history_mode="observed_system")
    assert resolve_alias(db_session, "Old primary company", as_of=before, **options)["status"] == "resolved"
    db_session.add(CatalogObservation(id=new_id(), source="opendart_corp_codes", identifier="00000001", content_hash="b" * 64,
        fields=changed_fields, captured_at=STAMP + timedelta(days=1)))
    db_session.flush()
    assert seed_aliases(db_session, manifest_id=manifest.id)["aliases_withdrawn"] == 1
    assert resolve_alias(db_session, "Old primary company", as_of=datetime.now(timezone.utc), **options)["status"] == "unresolved"
    assert resolve_alias(db_session, "Old primary company", as_of=before, **options)["status"] == "resolved"
    assert seed_aliases(db_session, manifest_id=manifest.id)["aliases_inserted"] == 0


def test_correction_to_outside_target_security_suppresses_old_target_alias(db_session):
    manifest = save_universe(db_session, [UniverseEntry("US", "NYSE", "OLD", "directory:OLD", "Old security", "common", {})],
                            output_start="2016-01-01", output_end="2026-10-07", evidence=[], gaps=[])
    target = db_session.get(Security, manifest.members[0]["security_id"])
    initial = register(db_session, target, identifier="OLD", venue="NYSE", assertion_key="reassigned")
    outside = security(db_session, code="outside", name="Outside target", market="US")
    register(db_session, outside, identifier="OLD", venue="NYSE", assertion_key="reassigned", recorded_at=STAMP + timedelta(days=2))
    result = resolve_alias(db_session, "OLD", manifest_id=manifest.id, as_of=STAMP + timedelta(days=5),
                           history_mode="observed_system")
    assert result["status"] == "unresolved"
    from backtest_data.catalog.aliases import resolve_records
    frozen_original = resolve_records("OLD", [initial], {target.id: target}, as_of=STAMP + timedelta(days=5),
                                      history_mode="observed_system")
    assert frozen_original["security_id"] == target.id


def test_concurrent_replay_from_separate_postgres_sessions_inserts_one_assertion(pg_engine):
    # A private test schema permits committed concurrent transactions without
    # leaking immutable evidence into the public fixture tables.
    schema = "alias_replay_" + new_id().replace("-", "")
    security_id = new_id()
    with pg_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'CREATE TABLE "{schema}".security (LIKE public.security INCLUDING ALL)'))
        connection.execute(text(f'CREATE TABLE "{schema}".identifier_history (LIKE public.identifier_history INCLUDING ALL)'))
    try:
        with pg_engine.connect() as connection:
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            with Session(connection) as session, session.begin():
                row = Security(id=security_id, market="KR", source_identifier="concurrent", security_type="common", name="Concurrent")
                session.add(row)
        def replay():
            with pg_engine.connect() as connection:
                connection.execute(text(f'SET search_path TO "{schema}"'))
                connection.commit()
                with Session(connection) as session, session.begin():
                    row = session.get(Security, security_id)
                    return register(session, row).id
        with ThreadPoolExecutor(max_workers=2) as workers:
            identities = list(workers.map(lambda _: replay(), range(2)))
        assert identities[0] == identities[1]
        with pg_engine.connect() as connection:
            assert connection.scalar(text(f'SELECT count(*) FROM "{schema}".identifier_history')) == 1
    finally:
        with pg_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
