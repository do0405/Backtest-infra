import importlib.util
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_price_coverage_index_is_partial_covering_concurrent_and_preserves_invalid_index():
    path = Path("alembic/versions/c71302d6b903_price_coverage_decision_index.py")
    assert path.is_file(), "covering index migration required"
    spec = importlib.util.spec_from_file_location("coverage_index_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "c71302d6b902"
    buffer = StringIO()
    module.op = Operations(MigrationContext.configure(dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer}))
    module.upgrade()
    sql = buffer.getvalue()
    assert "CREATE INDEX CONCURRENTLY" in sql
    assert "record_id, recorded_at DESC, id DESC" in sql
    assert "INCLUDE (status, rule_version, evidence_grade)" in sql
    assert "WHERE record_domain = 'price_observation'" in sql
    assert "ix_collection_job_coverage_entity" in sql and "ON collection_job (entity_id)" in sql
    assert "pg_index" in sql and "indisvalid" in sql
    assert sql.count("indisvalid") >= 3, "both indexes must be checked after the concurrent build"
    assert "DROP INDEX" not in sql
