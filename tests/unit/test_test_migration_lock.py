import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def fixture_module():
    spec=importlib.util.spec_from_file_location('test_fixture_helpers',Path('tests/conftest.py'))
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_waiting_migration_releases_its_snapshot_before_sleep():
    module=fixture_module()
    results=iter([False,True])
    pending=False
    commands=[]
    def execute(sql):
        nonlocal pending
        pending=True
        commands.append(str(sql))
        return SimpleNamespace(scalar_one=lambda:next(results))
    def commit():
        nonlocal pending
        pending=False
    def sleep(delay):
        assert pending is False,"waiting backend must not pin an old MVCC snapshot"
    connection=SimpleNamespace(execute=execute,commit=commit)
    module._acquire_test_migration_lock(connection,sleep=sleep,clock=lambda:0)
    assert pending is False
    assert len(commands)==2 and all('pg_try_advisory_lock' in q for q in commands)


def test_migration_lock_wait_is_bounded_and_leaves_no_snapshot():
    module=fixture_module()
    commits=[]
    connection=SimpleNamespace(execute=lambda sql:SimpleNamespace(scalar_one=lambda:False),commit=lambda:commits.append(1))
    clock=iter([0,301])
    with pytest.raises(TimeoutError):
        module._acquire_test_migration_lock(connection,sleep=lambda _:None,clock=lambda:next(clock))
    assert commits==[1]
