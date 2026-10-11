import importlib.util
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_online_read_indexes_reject_wrong_definitions_and_preserve_invalid_builds():
    path = Path("alembic/versions/c71302d6b916_price_financial_read_indexes.py")
    assert path.is_file(), "online covering index migration required"
    spec = importlib.util.spec_from_file_location("read_index_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "c71302d6b915"
    buffer = StringIO()
    module.op = Operations(MigrationContext.configure(dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer}))
    module.upgrade()
    sql = buffer.getvalue()
    assert sql.count("CREATE INDEX CONCURRENTLY IF NOT EXISTS") == 2
    assert "security_id, session, source_contract_id, captured_at DESC, id DESC" in sql
    assert "INCLUDE (basis, quantity_unit)" in sql
    assert "INCLUDE (basis, quantity_unit, open" not in sql
    assert "ON source_financial_fact (document_id, id)" in sql
    assert "INCLUDE (issuer_id, period_kind, period_start, period_end, instant, captured_at, extracted_at, first_seen_at)" in sql
    assert "indisvalid" in sql and "indisready" in sql and "indislive" in sql
    assert "indnkeyatts" in sql and "indnatts" in sql and "indoption" in sql
    assert "indpred IS NOT NULL" in sql and "indexprs IS NOT NULL" in sql
    assert "pg_attribute" in sql and "pg_opclass" in sql and "pg_collation" in sql
    assert sql.count("preserve and inspect") >= 4
    assert "DROP INDEX" not in sql
    assert "UPDATE " not in sql and "DELETE " not in sql
