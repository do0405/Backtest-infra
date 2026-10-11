import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from backtest_data.ops.postgres_tuning import RELOAD_PROFILE


@pytest.fixture
def operation(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("live_tune", Path("artifacts/db-performance-20261011/tune.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    marker = {"system_id": module.SYSTEM_ID, "started_at": "unchanged",
              "price_marker": "same", "release_hashes": [], "manifest_hashes": []}
    values = {"wal_compression": "off", "checkpoint_timeout": "5min", "max_wal_size": "1GB",
              "min_wal_size": "80MB", "work_mem": "4MB", "effective_cache_size": "4GB",
              "track_io_timing": "off", "track_wal_io_timing": "off",
              "fsync": "on", "synchronous_commit": "on", "full_page_writes": "on"}
    original = {k: {"value": v, "context": "user"} for k, v in values.items()}
    statements = []
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execution_options(self, **kwargs): return self
        def execute(self, sql, params=None):
            statements.append(str(sql))
            return SimpleNamespace(all=lambda: [], scalar_one=lambda: True)
    connection = Connection()
    monkeypatch.setattr(module, "create_engine", lambda *args, **kwargs: SimpleNamespace(connect=lambda: connection))
    monkeypatch.setattr(module.Settings, "from_env", lambda: SimpleNamespace(database_url="postgresql://test"))
    monkeypatch.setattr(module, "configuration", lambda c: original)
    monkeypatch.setattr(module, "identity", lambda c: marker)
    monkeypatch.setattr(module, "counters", lambda c: {})
    monkeypatch.setattr(module.shutil, "disk_usage", lambda p: SimpleNamespace(free=200 * 1024 ** 3))
    monkeypatch.setattr(module, "RECEIPT", tmp_path / "receipt.json")
    monkeypatch.setattr("sys.argv", ["tune", "--apply"])
    return module, statements, marker


def test_receipt_failure_after_alter_still_restores_configuration(operation, monkeypatch):
    module, statements, _ = operation
    writes = 0
    def save(value):
        nonlocal writes
        writes += 1
        if writes >= 3:
            raise OSError("receipt disk failure")
    monkeypatch.setattr(module, "save", save)
    with pytest.raises(OSError):
        module.main()
    assert "ALTER SYSTEM SET wal_compression TO 'on'" in statements
    assert "ALTER SYSTEM RESET wal_compression" in statements
    assert "SELECT pg_reload_conf()" in statements
    assert not any("UPDATE " in sql or "DELETE " in sql for sql in statements)


def test_verified_receipt_drift_is_rejected_before_configuration_write(operation):
    import json
    module, statements, marker = operation
    module.RECEIPT.write_text(json.dumps({"phase": "verified", "before_identity": marker}), encoding="utf-8")
    with pytest.raises(ValueError, match="drift"):
        module.main()
    assert not any("ALTER SYSTEM" in sql for sql in statements)


def test_reload_refusal_is_not_success(operation):
    module, _, _ = operation
    connection = SimpleNamespace(execute=lambda sql: SimpleNamespace(scalar_one=lambda: False))
    with pytest.raises(ValueError, match="rejected"):
        module.reload_and_observe(connection, RELOAD_PROFILE)
