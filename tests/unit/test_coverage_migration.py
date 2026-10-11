import importlib.util
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_coverage_migration_is_additive_and_does_not_mutate_observations():
    path = Path("alembic/versions/c71302d6b902_whole_universe_coverage.py")
    assert path.is_file(), "SQL coverage migration is required"
    spec = importlib.util.spec_from_file_location("coverage_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "b830df281926"
    buffer = StringIO()
    module.op = Operations(MigrationContext.configure(dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer}))
    module.upgrade()
    sql = buffer.getvalue()
    for name in ("coverage_run", "coverage_member", "coverage_summary", "coverage_gap", "coverage_financial_period"):
        assert "CREATE TABLE " + name in sql
    assert "ALTER TABLE price_observation" not in sql
    assert "ALTER TABLE source_financial_fact" not in sql
    buffer.seek(0)
    buffer.truncate(0)
    module.downgrade()
    assert "DROP TABLE coverage_run" in buffer.getvalue()
