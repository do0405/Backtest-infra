"""A fixed price audit needs one snapshot and a bounded index payload."""
from datetime import date, datetime, timezone
import importlib.util
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql

from backtest_data.coverage import service


def test_price_dates_use_one_statement_and_keep_observed_rows_when_no_clean_capture():
    statements = []
    class Session:
        def execute(self, statement):
            statements.append(statement)
            rows = [SimpleNamespace(security_id="one", session=date(2026, 10, 1), rows=3, clean_rows=1),
                    SimpleNamespace(security_id="two", session=date(2026, 10, 2), rows=2, clean_rows=0)]
            return SimpleNamespace(yield_per=lambda size: rows)
    run = SimpleNamespace(output_start=date(2016, 1, 1), output_end=date(2026, 10, 7),
                          watermark=datetime(2026, 10, 8, tzinfo=timezone.utc))
    result = service._dates(Session(), run, ("one", "two"), "prices")
    assert len(statements) == 1
    sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "WITH observed_price_dates AS" in sql
    assert "clean_price_dates AS" in sql and "LEFT OUTER JOIN clean_price_dates" in sql
    assert "clean_price_dates AS MATERIALIZED" in sql, "generic plans must not rescan the clean aggregate per observed date"
    assert "'as_traded'" in sql and "'shares'" in sql
    assert "'NaN'" in sql and "'Infinity'" in sql and "'-Infinity'" in sql
    assert "price_observation.open > 0" in sql or "open > 0" in sql
    assert sql.index("ORDER BY selection_decision.recorded_at DESC, selection_decision.id DESC") < sql.index("latest_price_decision.status")
    assert result["one"] == {"observed": {date(2026, 10, 1)}, "clean": {date(2026, 10, 1)}, "source_rows": 3}
    assert result["two"] == {"observed": {date(2026, 10, 2)}, "clean": set(), "source_rows": 2}


def test_empty_price_security_batch_avoids_any_sql():
    assert service._dates(None, None, (), "prices") == {}


def test_structural_partial_index_is_concurrent_narrow_and_rejects_other_predicates():
    path = Path("alembic/versions/c71302d6b917_price_structure_read_index.py")
    assert path.is_file(), "bounded structural price index migration required"
    spec = importlib.util.spec_from_file_location("price_structure_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "c71302d6b916"
    buffer = StringIO()
    module.op = Operations(MigrationContext.configure(dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer}))
    module.upgrade()
    sql = buffer.getvalue()
    assert sql.count("CREATE INDEX CONCURRENTLY IF NOT EXISTS") == 1
    assert "ON price_observation (security_id, session, captured_at, id)" in sql
    assert "INCLUDE" not in sql
    assert "pg_get_expr" in sql and "indisvalid" in sql and "indisready" in sql and "indislive" in sql
    assert "pg_opclass" in sql and "indnkeyatts=4" in sql and "indnatts=4" in sql
    assert "preserve and inspect" in sql
    assert "DROP INDEX" not in sql and "UPDATE " not in sql and "DELETE " not in sql
    assert module.PREDICATE_SQL == service.PRICE_STRUCTURE_PREDICATE
