"""Review the additive PostgreSQL DDL without opening any database."""
import importlib.util
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_publication_migration_is_additive_immutable_and_reversible():
    path = Path("alembic/versions/b830df281926_financial_publication_evidence.py")
    assert path.is_file(), "publication migration is required"
    spec = importlib.util.spec_from_file_location("publication_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "6c347adb8181"
    buffer = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer})
    module.op = Operations(context)
    module.upgrade()
    upgrade = buffer.getvalue()
    assert "CREATE TABLE financial_publication_evidence" in upgrade
    assert "BEFORE UPDATE OR DELETE ON financial_publication_evidence" in upgrade
    assert "EXECUTE FUNCTION backtest_immutable()" in upgrade
    assert "ALTER TABLE filing" not in upgrade
    assert "ALTER TABLE financial_source_document" not in upgrade
    buffer.seek(0)
    buffer.truncate(0)
    module.downgrade()
    downgrade = buffer.getvalue()
    assert "DROP TRIGGER immutable_evidence ON financial_publication_evidence" in downgrade
    assert "DROP TABLE financial_publication_evidence" in downgrade
    assert "DROP FUNCTION backtest_immutable" not in downgrade
