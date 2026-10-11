import importlib.util
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_annotation_lookup_index_is_additive_concurrent_and_checks_validity():
    path = Path("alembic/versions/c71302d6b904_financial_annotation_index.py")
    assert path.is_file(), "annotation index migration required"
    spec = importlib.util.spec_from_file_location("annotation_index_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "c71302d6b903"
    buffer = StringIO()
    module.op = Operations(MigrationContext.configure(dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer}))
    module.upgrade()
    sql = buffer.getvalue()
    assert "CREATE INDEX CONCURRENTLY" in sql
    assert "ON validation_issue (record_domain, record_id, rule, created_at DESC, id DESC)" in sql
    assert "indisvalid" in sql and "DROP INDEX" not in sql
    assert sql.count("indisvalid") >= 2, "verify validity after CREATE IF NOT EXISTS before stamping head"
