import os
from pathlib import Path
import time

import pytest


def _acquire_test_migration_lock(connection, *, timeout=300, sleep=time.sleep, clock=time.monotonic):
    from sqlalchemy import text
    deadline=clock()+timeout
    while True:
        acquired=connection.execute(text("SELECT pg_try_advisory_lock(hashtext('backtest_test_migrations_v1'))")).scalar_one()
        # A blocking advisory-lock query can pin the snapshot that another
        # session's concurrent index build is waiting to retire. Commit each
        # failed try; the winning session lock survives the commit.
        connection.commit()
        if acquired:return
        if clock()>=deadline:raise TimeoutError('test migration lock wait exceeded')
        sleep(0.1)


def pytest_addoption(parser):
    parser.addoption("--require-postgres", action="store_true", help="Fail if PostgreSQL is unavailable")
    parser.addoption("--run-live", action="store_true", help="Enable explicitly requested network checks")


@pytest.fixture
def pg_engine(request):
    from backtest_data.config import load_environment
    from backtest_data.db import make_engine

    load_environment(Path(".env"))
    url = os.environ.get("BACKTEST_TEST_DATABASE_URL")
    if not url:
        if request.config.getoption("--require-postgres"):
            pytest.fail("BACKTEST_TEST_DATABASE_URL is required; SQLite cannot replace PostgreSQL")
        pytest.skip("PostgreSQL integration tests require BACKTEST_TEST_DATABASE_URL")
    if not url.startswith("postgresql") or not url.rsplit("/", 1)[-1].split("?", 1)[0].endswith("_test"):
        pytest.fail("Integration tests require a separate PostgreSQL database ending in _test")
    engine = make_engine(url)
    connection_available = True
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
    except Exception:
        connection_available = False
    if not connection_available:
        engine.dispose()
        pytest.fail("PostgreSQL test connection unavailable (credentials redacted)")
    from alembic import command
    from alembic.config import Config
    with engine.connect() as connection:
        from sqlalchemy import text
        # Session lock survives CONCURRENTLY migration autocommit blocks.
        _acquire_test_migration_lock(connection)
        try:
            cfg = Config("alembic.ini")
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
            connection.commit()
        finally:
            if connection.in_transaction():connection.rollback()
            connection.execute(text("SELECT pg_advisory_unlock(hashtext('backtest_test_migrations_v1'))"))
            connection.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(pg_engine):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.orm import Session

    with pg_engine.connect() as connection:
        tx = connection.begin()
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            yield session
        tx.rollback()
