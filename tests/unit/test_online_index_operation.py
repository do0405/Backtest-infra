import importlib.util
from pathlib import Path


def operation():
    path=Path('artifacts/db-performance-20261011/apply-read-indexes.py')
    spec=importlib.util.spec_from_file_location('online_index_operation_test',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GuardConnection:
    def __init__(self):self.statements=[]
    def execute(self,statement):self.statements.append(str(statement))


def test_already_applied_schema_still_verifies_full_index_definitions():
    module=operation()
    connection=GuardConnection()
    module.verify_index_definitions(connection,'c71302d6b916')
    assert len(connection.statements)==2
    assert all('definition differs; preserve and inspect' in sql for sql in connection.statements)
    assert all('missing; preserve and inspect' in sql for sql in connection.statements)
    assert all('indoption' in sql and 'pg_opclass' in sql for sql in connection.statements)
    assert all('CREATE INDEX' not in sql and 'DROP INDEX' not in sql for sql in connection.statements)


def test_followup_verifies_both_prior_indexes_and_exact_partial_predicate():
    module=operation()
    connection=GuardConnection()
    module.verify_index_definitions(connection,'c71302d6b917')
    assert len(connection.statements)==3
    assert all('missing; preserve and inspect' in sql for sql in connection.statements)
    assert 'ix_price_observation_clean_structure_read' in connection.statements[-1]
    assert 'pg_get_expr' in connection.statements[-1]
    assert all('CREATE INDEX' not in sql and 'DROP INDEX' not in sql for sql in connection.statements)
