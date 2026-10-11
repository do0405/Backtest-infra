"""Prove structural coverage stays exact while both reads avoid price bodies."""
from datetime import date, timedelta
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import random
from types import SimpleNamespace

import pytest
from sqlalchemy import and_, cast, func, literal_column, select, String, text, true
from sqlalchemy.exc import DBAPIError

from backtest_data.coverage import service
from backtest_data.catalog.models import SourceContract
from backtest_data.db import new_id
from backtest_data.ingestion.models import RawObject
from backtest_data.prices.models import PriceObservation
from backtest_data.releases.models import SelectionDecision
from test_coverage_sql import target, CAPTURE, START, END


pytestmark = pytest.mark.integration


def add_price(session, security_id, day, *, basis="as_traded", capture=CAPTURE, volume=100,
              rule="raw_price_structure_v1", grade="contract_verified", **fields):
    """Append initial test values; immutable evidence is never edited."""
    contract = SourceContract(source="coverage_structure_test", version=new_id(), domain="prices", market="US",
        specification={}, qualification="pending", content_hash="a" * 64)
    raw = RawObject(content_hash=new_id().replace("-", "") * 2, storage_key=new_id(), byte_count=1)
    session.add_all([contract, raw])
    session.flush()
    values = dict(security_id=security_id, source_contract_id=contract.id, raw_id=raw.id, session=day,
        basis=basis, feed="sip", venue="US", trade_scope="session_total", currency="USD", quantity_unit="shares",
        open=10, high=12, low=9, close=11, volume=volume, notional=None,
        signature=new_id().replace("-", "") * 2, available_at=capture, captured_at=capture, evidence={})
    values.update(fields)
    row = PriceObservation(**values)
    session.add(row)
    session.flush()
    session.add(SelectionDecision(record_domain="price_observation", record_id=row.id, status="accepted",
        rule_version=rule, evidence_grade=grade, reason="test structure", actor="coverage_test", recorded_at=CAPTURE))
    session.flush()
    return row


def reference_statement(run, security_ids):
    """Freeze the original one-pass predicate as a regression oracle."""
    model = PriceObservation
    latest = select(SelectionDecision.status, SelectionDecision.rule_version, SelectionDecision.evidence_grade).where(
        SelectionDecision.record_domain == literal_column("'price_observation'"),
        SelectionDecision.record_id == model.id, SelectionDecision.recorded_at <= run.watermark).order_by(
            SelectionDecision.recorded_at.desc(), SelectionDecision.id.desc()).limit(1).lateral("old_latest")
    valid = and_(latest.c.status == "accepted", latest.c.evidence_grade == "contract_verified",
        latest.c.rule_version.in_(("raw_price_structure_v1", "alpaca_raw_price_structure_v1", "kis_overseas_raw_price_structure_v1")),
        model.basis == "as_traded", model.quantity_unit == "shares", model.open > 0, model.close > 0,
        model.low > 0, model.high >= model.low, model.high >= model.open, model.high >= model.close,
        model.low <= model.open, model.low <= model.close, model.volume.is_not(None), model.volume >= 0,
        *(cast(column, String).not_in(("NaN", "Infinity", "-Infinity")) for column in
          (model.open, model.high, model.low, model.close, model.volume)))
    return select(model.security_id, model.session, func.count().label("rows"),
        func.count().filter(valid).label("clean_rows")).select_from(model.__table__.outerjoin(latest, true())).where(
            model.security_id.in_(security_ids), model.session >= run.output_start, model.session <= run.output_end,
            model.captured_at <= run.watermark).group_by(model.security_id, model.session)


def normalized(rows):
    result = {}
    for row in rows:
        value = result.setdefault(row.security_id, {"observed": set(), "clean": set(), "source_rows": 0})
        value["observed"].add(row.session)
        value["source_rows"] += row.rows
        if row.clean_rows:
            value["clean"].add(row.session)
    return result


def test_split_price_dates_match_original_with_supersession_nonfinite_and_huge_numeric(db_session):
    assert db_session.scalar(text("SELECT version_num FROM alembic_version")) == "c71302d6b917"
    db_session.execute(text(migration()._guard(required=True)))
    manifest = target(db_session, ("etf", "etf", "etf"), missing=False)
    sid, invalid_sid, empty_sid = [member["security_id"] for member in manifest.members]
    run = SimpleNamespace(output_start=START, output_end=END, watermark=CAPTURE + timedelta(seconds=10))
    first = add_price(db_session, sid, START, rule="kis_overseas_raw_price_structure_v1")
    db_session.add(SelectionDecision(record_domain="price_observation", record_id=first.id, status="rejected",
        rule_version="correction", evidence_grade="invalid", reason="after frozen watermark", actor="test",
        recorded_at=run.watermark + timedelta(seconds=1)))
    duplicate = add_price(db_session, sid, START)
    db_session.add(SelectionDecision(record_domain="price_observation", record_id=duplicate.id, status="rejected",
        rule_version="correction", evidence_grade="invalid", reason="later rejection", actor="test",
        recorded_at=CAPTURE + timedelta(seconds=1)))
    add_price(db_session, sid, date(2026, 10, 2), basis="adjusted")
    add_price(db_session, sid, date(2026, 10, 3), volume=None)
    add_price(db_session, sid, date(2026, 10, 4), volume="Infinity")
    rejected = add_price(db_session, sid, date(2026, 10, 5))
    for identity, status, offset in [("a" * 36, "accepted", 2), ("b" * 36, "rejected", 2), ("c" * 36, "accepted", 20)]:
        db_session.add(SelectionDecision(id=identity, record_domain="price_observation", record_id=rejected.id,
            status=status, rule_version="raw_price_structure_v1", evidence_grade="contract_verified",
            reason="watermark/tied-order check", actor="test", recorded_at=CAPTURE + timedelta(seconds=offset)))
    digits = "".join(random.Random(19).choices("0123456789", k=9000))
    amount = Decimal("1." + digits)
    huge = add_price(db_session, sid, date(2026, 10, 6), volume=0, rule="alpaca_raw_price_structure_v1",
        open=amount, high=amount, low=amount, close=amount)
    add_price(db_session, sid, date(2026, 10, 7), rule="kis_supplier_zero_transaction_structure_v1",
        grade="supplier_reported_unverified", volume=0)
    add_price(db_session, sid, date(2026, 10, 8))
    add_price(db_session, sid, START, capture=run.watermark + timedelta(seconds=1))
    add_price(db_session, invalid_sid, START, volume="NaN")
    add_price(db_session, invalid_sid, date(2026, 10, 2), volume="-Infinity")
    add_price(db_session, invalid_sid, date(2026, 10, 3),
        open=Decimal("NaN"), high=Decimal("NaN"), low=Decimal("NaN"), close=Decimal("NaN"))
    add_price(db_session, invalid_sid, date(2026, 10, 4), volume=-1)
    add_price(db_session, invalid_sid, date(2026, 10, 5), open=0)
    add_price(db_session, invalid_sid, date(2026, 10, 6), high=8)
    add_price(db_session, invalid_sid, date(2026, 10, 7), quantity_unit="contracts")
    add_price(db_session, invalid_sid, date(2026, 10, 7), rule="unregistered_structure_rule")
    db_session.flush()
    ids = (sid, invalid_sid, empty_sid)
    expected = normalized(db_session.execute(reference_statement(run, ids)))
    actual = service._dates(db_session, run, ids, "prices")
    assert dict(actual) == expected
    assert actual[sid]["source_rows"] == 8
    assert actual[sid]["observed"] == {date(2026, 10, day) for day in range(1, 8)}
    assert actual[sid]["clean"] == {START, date(2026, 10, 6)}
    assert actual[invalid_sid]["clean"] == set() and empty_sid not in actual
    db_session.expire(huge)
    assert huge.open.as_tuple() == amount.as_tuple(), "index must not narrow valid source precision"


def nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from nodes(child)


def migration():
    path = Path("alembic/versions/c71302d6b917_price_structure_read_index.py")
    spec = importlib.util.spec_from_file_location("structure_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_structural_index_guard_rejects_missing_or_wrong_same_name_definition(pg_engine):
    module = migration()
    with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        assert connection.scalar(text("SELECT current_database()")).endswith("_test")
        connection.execute(text("SET search_path=pg_temp,pg_catalog"))
        connection.execute(text("CREATE TEMP TABLE price_observation (LIKE public.price_observation INCLUDING DEFAULTS INCLUDING CONSTRAINTS)"))
        connection.execute(text(module._guard(required=False)))
        with pytest.raises(DBAPIError, match="missing; preserve and inspect"):
            connection.execute(text(module._guard(required=True)))
        definitions = [
            "(security_id,session,captured_at,id) WHERE volume>=0",
            "(security_id,session,captured_at,id)",
            "(security_id,session,captured_at DESC,id) WHERE " + module.PREDICATE_SQL,
            "(session,security_id,captured_at,id) WHERE " + module.PREDICATE_SQL,
            "(security_id,session,captured_at,id) INCLUDE (basis) WHERE " + module.PREDICATE_SQL,
            "(security_id text_pattern_ops,session,captured_at,id) WHERE " + module.PREDICATE_SQL,
            '(security_id COLLATE "C",session,captured_at,id) WHERE ' + module.PREDICATE_SQL,
            "(lower(security_id),session,captured_at,id) WHERE " + module.PREDICATE_SQL,
        ]
        for definition in definitions:
            connection.execute(text(f"CREATE INDEX {module.INDEX_NAME} ON price_observation " + definition))
            with pytest.raises(DBAPIError, match="invalid or definition differs; preserve and inspect"):
                connection.execute(text(module._guard(required=False)))
            assert connection.scalar(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": module.INDEX_NAME})
            connection.execute(text("DROP INDEX pg_temp." + module.INDEX_NAME))
        connection.execute(text(f"CREATE INDEX {module.INDEX_NAME} ON price_observation "
            "(security_id,session,captured_at,id) WHERE " + module.PREDICATE_SQL))
        connection.execute(text(module._guard(required=True)))


def test_generic_price_plan_uses_bounded_index_only_scans(pg_engine, tmp_path):
    # Temporary tables isolate this EXPLAIN fixture from all persistent source
    # rows. PostgreSQL always builds temporary-table indexes non-concurrently;
    # migration concurrency is verified independently by migration tests.
    with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        assert connection.scalar(text("SELECT current_database()" )).endswith("_test")
        connection.execute(text("SET statement_timeout='30s'"))
        connection.execute(text("CREATE TEMP TABLE price_observation (LIKE public.price_observation INCLUDING DEFAULTS INCLUDING CONSTRAINTS)"))
        connection.execute(text("CREATE TEMP TABLE selection_decision (LIKE public.selection_decision INCLUDING DEFAULTS INCLUDING CONSTRAINTS)"))
        connection.execute(text("""INSERT INTO price_observation
            (id,security_id,source_contract_id,raw_id,session,basis,feed,venue,trade_scope,currency,quantity_unit,
             open,high,low,close,volume,notional,signature,available_at,captured_at,evidence)
            SELECT md5(g::text), CASE WHEN g<=100 THEN 'test-price-plan' ELSE md5((g%100)::text) END,
                'contract','raw',DATE '2026-10-01'+(g%5),'as_traded','sip','US','session_total','USD','shares',
                10,12,9,11,100,NULL,md5(g::text)||md5(g::text),:captured,:captured,
                jsonb_build_object('pad',repeat('x',1024)) FROM generate_series(1,10000) g"""), {"captured": CAPTURE})
        connection.execute(text("""INSERT INTO selection_decision
            (id,record_domain,record_id,status,rule_version,evidence_grade,reason,actor,recorded_at)
            SELECT md5('decision'||id),'price_observation',id,'accepted','raw_price_structure_v1',
                'contract_verified','test','test',captured_at FROM price_observation"""))
        connection.execute(text("CREATE INDEX ix_test_price_observed ON price_observation (security_id,session,source_contract_id,captured_at DESC,id DESC) INCLUDE (basis,quantity_unit)"))
        connection.execute(text("CREATE INDEX ix_test_price_structure ON price_observation (security_id,session,captured_at,id) WHERE " + service.PRICE_STRUCTURE_PREDICATE))
        connection.execute(text("CREATE INDEX ix_test_price_decision ON selection_decision (record_id,recorded_at DESC,id DESC) INCLUDE (status,rule_version,evidence_grade) WHERE record_domain='price_observation'"))
        # Temp-table vacuum shares the slow disk with the authorized live build.
        # All other fixture statements and actual queries retain their timeout.
        connection.execute(text("SET statement_timeout='300s'"))
        connection.execute(text("VACUUM ANALYZE pg_temp.price_observation"))
        connection.execute(text("VACUUM ANALYZE pg_temp.selection_decision"))
        connection.execute(text("SET statement_timeout='30s'"))
        run = SimpleNamespace(output_start=START, output_end=END, watermark=CAPTURE + timedelta(seconds=10))
        statement = service._price_dates_statement(run, ("test-price-plan",))
        original_sql = str(reference_statement(run, ("test-price-plan",)).compile(
            dialect=connection.dialect, compile_kwargs={"literal_binds": True}))
        original_document = connection.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + original_sql).scalar_one()
        original_scans = [node for node in nodes(original_document[0]["Plan"]) if node.get("Relation Name") == "price_observation"]
        assert any(node["Node Type"] in ("Index Scan", "Bitmap Heap Scan", "Seq Scan") for node in original_scans)
        default_sql = str(statement.compile(dialect=connection.dialect, compile_kwargs={"literal_binds": True}))
        default_document = connection.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + default_sql).scalar_one()
        default_scans = [node for node in nodes(default_document[0]["Plan"]) if node.get("Relation Name") == "price_observation"]
        assert {node.get("Index Name") for node in default_scans} == {"ix_test_price_observed", "ix_test_price_structure"}
        assert all(node["Node Type"] == "Index Only Scan" and node.get("Heap Fetches") == 0 for node in default_scans)
        assert all(node["Actual Loops"] == 1 for node in default_scans)
        # Keep security/date/watermark values as parameters in a forced generic
        # server PREPARE; partial-index constants must still be fixed SQL.
        compiled = statement.compile(dialect=connection.dialect, compile_kwargs={"render_postcompile": True})
        parameter_names = list(compiled.params)
        sql = str(compiled)
        for ordinal, name in enumerate(parameter_names, 1):
            sql = sql.replace("%(" + name + ")s", "$" + str(ordinal))
        connection.execute(text("SET plan_cache_mode=force_generic_plan"))
        connection.exec_driver_sql("PREPARE coverage_price_plan AS " + sql)
        literals = []
        from sqlalchemy import literal
        for name in parameter_names:
            literals.append(str(literal(compiled.params[name]).compile(dialect=connection.dialect,
                compile_kwargs={"literal_binds": True})))
        execute = "EXECUTE coverage_price_plan(" + ",".join(literals) + ")"
        document = connection.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + execute).scalar_one()
        plan = document[0]["Plan"]
        scans = [node for node in nodes(plan) if node.get("Relation Name") == "price_observation"]
        assert {node.get("Index Name") for node in scans} == {"ix_test_price_observed", "ix_test_price_structure"}
        assert all(node["Node Type"] == "Index Only Scan" and node.get("Heap Fetches") == 0 for node in scans)
        assert all(node["Actual Loops"] == 1 for node in scans), "one clean aggregate pass per statement"
        decision_scans = [node for node in nodes(plan) if node.get("Index Name") == "ix_test_price_decision"]
        assert sum(node["Actual Loops"] for node in decision_scans) == 100
        assert sum(row.rows for row in connection.exec_driver_sql(execute)) == 100
        evidence = {"original_plan": original_document, "default_plan": default_document, "generic_plan": document,
                    "scan_forcing_used": False, "source_rows": 100}
        (tmp_path / "coverage-price-explain.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
