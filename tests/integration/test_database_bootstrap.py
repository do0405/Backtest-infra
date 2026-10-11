import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text


pytestmark = pytest.mark.integration


def test_transaction_rollback_and_migration_roundtrip(pg_engine):
    cfg = Config("alembic.ini")
    with pg_engine.connect() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
        assert "source_contract" in inspect(connection).get_table_names()
        connection.commit()
        with connection.begin():
            connection.execute(text("CREATE TEMP TABLE rollback_probe (value integer)"))
        tx = connection.begin()
        connection.execute(text("INSERT INTO rollback_probe VALUES (7)"))
        tx.rollback()
        assert connection.scalar(text("SELECT count(*) FROM rollback_probe")) == 0
        connection.commit()
        command.downgrade(cfg, "base")
        assert "source_contract" not in inspect(connection).get_table_names()
        # Catalog inspection autobegins a transaction; concurrent index DDL
        # requires the next Alembic run to own its transaction boundary.
        connection.commit()
        command.upgrade(cfg, "head")
        connection.commit()
        assert "source_contract" in inspect(connection).get_table_names()
