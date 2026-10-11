import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError,PendingRollbackError

from backtest_data.ops.db_recovery import database_error_payload

pytestmark=pytest.mark.integration


def test_only_own_test_connection_is_disconnected_and_new_connection_recovers(pg_engine):
    victim=pg_engine.connect()
    try:
        pid=victim.scalar(text('SELECT pg_backend_pid()'))
        with pg_engine.begin() as control:
            assert control.scalar(text('SELECT current_database()')).endswith('_test')
            assert control.scalar(text('SELECT pg_terminate_backend(:pid)'),{'pid':pid}) is True
        with pytest.raises(OperationalError) as caught:victim.execute(text('SELECT 1'))
        assert database_error_payload(caught.value)['retryable'] is True
        with pytest.raises(PendingRollbackError) as pending:victim.execute(text('SELECT 1'))
        assert pending.value.code=='8s2b'
        assert database_error_payload(pending.value)['retryable'] is True
        with pg_engine.connect() as recovered:assert recovered.scalar(text('SELECT 1'))==1
    finally:victim.close()
