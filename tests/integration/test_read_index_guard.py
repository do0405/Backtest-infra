import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


def migration():
    spec=importlib.util.spec_from_file_location('read_index',Path('alembic/versions/c71302d6b916_price_financial_read_indexes.py'))
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_wrong_definition_is_rejected_and_transaction_preserves_original(pg_engine):
    module=migration()
    spec=module.INDEXES[0]
    with pg_engine.connect() as connection:
        tx=connection.begin()
        connection.exec_driver_sql(module._guard(spec,required=True))
        connection.exec_driver_sql('DROP INDEX ix_price_observation_coverage_read')
        connection.exec_driver_sql('CREATE INDEX ix_price_observation_coverage_read ON price_observation (security_id)')
        with pytest.raises(DBAPIError,match='definition differs'):
            connection.exec_driver_sql(module._guard(spec,required=False))
        tx.rollback()
        connection.exec_driver_sql(module._guard(spec,required=True))


def test_missing_completed_index_cannot_be_marked_success(pg_engine):
    module=migration()
    spec=module.INDEXES[1]
    with pg_engine.connect() as connection:
        tx=connection.begin()
        connection.exec_driver_sql('DROP INDEX ix_source_financial_fact_period_read')
        with pytest.raises(DBAPIError,match='missing'):
            connection.exec_driver_sql(module._guard(spec,required=True))
        tx.rollback()
        connection.exec_driver_sql(module._guard(spec,required=True))


def test_period_and_price_window_plans_have_covering_seek_paths(pg_engine):
    with pg_engine.connect() as connection:
        connection.exec_driver_sql('SET enable_seqscan=off')
        connection.exec_driver_sql('SET enable_bitmapscan=off')
        fact=connection.execute(text("EXPLAIN (FORMAT JSON) SELECT id,issuer_id,period_kind,period_start,period_end,instant,captured_at,extracted_at,first_seen_at FROM source_financial_fact WHERE document_id='read-plan-probe' ORDER BY id")).scalar_one()
        price=connection.execute(text("EXPLAIN (FORMAT JSON) SELECT id,captured_at,basis,quantity_unit FROM price_observation WHERE security_id='read-plan-probe' AND session BETWEEN '2016-01-01' AND '2026-10-07' AND source_contract_id='read-plan-contract' ORDER BY session")).scalar_one()
        def nodes(plan):
            yield plan
            for child in plan.get('Plans',[]):yield from nodes(child)
        assert any(n.get('Node Type')=='Index Only Scan' and n.get('Index Name')=='ix_source_financial_fact_period_read' for n in nodes(fact[0]['Plan']))
        assert any(n.get('Node Type')=='Index Only Scan' and n.get('Index Name')=='ix_price_observation_coverage_read' for n in nodes(price[0]['Plan']))
