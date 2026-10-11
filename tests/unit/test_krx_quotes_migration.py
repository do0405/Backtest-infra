import importlib.util
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations

from backtest_data.db import Base
from backtest_data.prices.krx_quote_models import KRXQuoteObservation


def test_krx_stage_migration_creates_only_new_immutable_stage_with_matching_constraints():
    path = Path("alembic/versions/c71302d6b905_krx_quote_stage.py")
    assert path.is_file(), "KRX quote migration required"
    spec = importlib.util.spec_from_file_location("krx_quote_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "c71302d6b904"
    buffer = StringIO()
    module.op = Operations(MigrationContext.configure(dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer, "target_metadata": Base.metadata}))
    module.upgrade()
    sql = buffer.getvalue()
    assert sql.count("CREATE TABLE") == 1 and "CREATE TABLE krx_quote_observation" in sql
    assert "BEFORE UPDATE OR DELETE ON krx_quote_observation" in sql and "backtest_immutable()" in sql
    assert "UPDATE price_observation" not in sql and "DELETE FROM" not in sql and "ALTER TABLE" not in sql
    for constraint in KRXQuoteObservation.__table__.constraints:
        assert "CONSTRAINT " + constraint.name + " " in sql
