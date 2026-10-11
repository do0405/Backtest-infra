import json
from types import SimpleNamespace

import psycopg
import pytest


@pytest.mark.parametrize('message',[
    'FATAL: the database system is starting up',
    'FATAL: the database system is not yet accepting connections',
    'FATAL: the database system is in recovery mode',
    'FATAL: the database system is shutting down',
])
def test_startup_driver_without_sqlstate_is_transient(message):
    import psycopg
    from sqlalchemy.exc import OperationalError
    from backtest_data.ops.db_recovery import database_error_payload
    result=database_error_payload(OperationalError(None,None,psycopg.OperationalError(message)))
    assert result['retryable'] is True and result['sqlstate'] is None


def test_disconnected_invalid_transaction_differs_from_failed_sql_transaction():
    from sqlalchemy.exc import PendingRollbackError
    from backtest_data.ops.db_recovery import database_error_payload,AttemptResult,retryable_result
    transport=PendingRollbackError("Can't reconnect until invalid transaction is rolled back",code='8s2b')
    diagnostic=database_error_payload(transport)
    assert diagnostic['retryable'] is True
    assert retryable_result(AttemptResult(2,{'status':'blocked','reason':'PendingRollbackError','database_error':diagnostic}))
    permanent=PendingRollbackError('Previous failed statement requires rollback',code='7s2a')
    assert database_error_payload(permanent)['retryable'] is False
from sqlalchemy.exc import OperationalError, ProgrammingError


@pytest.mark.parametrize('code',['08006','08003','57P01','57P02','57P03'])
def test_known_disconnect_and_recovery_states_are_retryable_without_error_text(code):
    from backtest_data.ops.db_recovery import database_error_payload
    orig=Exception('PRIVATE_KEY and private SQL params')
    orig.sqlstate=code
    payload=database_error_payload(OperationalError('private statement',{'secret':'PRIVATE_KEY'},orig))
    assert payload['retryable'] is True and payload['sqlstate']==code
    assert 'PRIVATE_KEY' not in json.dumps(payload)


@pytest.mark.parametrize('code',['28P01','42501','42601','53200','23505'])
def test_permanent_or_resource_errors_do_not_retry_even_with_invalidated_connection(code):
    from backtest_data.ops.db_recovery import database_error_payload
    orig=Exception('connection refused PRIVATE_KEY')
    orig.sqlstate=code
    payload=database_error_payload(OperationalError(None,None,orig,connection_invalidated=True))
    assert payload['retryable'] is False


def test_unknown_operational_error_does_not_turn_into_unconditional_retry():
    from backtest_data.ops.db_recovery import database_error_payload
    assert database_error_payload(OperationalError(None,None,Exception('unknown failure')))['retryable'] is False
    assert database_error_payload(ProgrammingError(None,None,Exception('connection refused')))['retryable'] is False


def test_driver_connection_refused_without_sqlstate_is_recognized_and_redacted():
    from backtest_data.ops.db_recovery import database_error_payload
    payload=database_error_payload(OperationalError(None,None,psycopg.OperationalError('connection refused PRIVATE_KEY')))
    assert payload['retryable'] is True and payload['sqlstate'] is None
    assert 'PRIVATE_KEY' not in json.dumps(payload)


@pytest.mark.parametrize('message',[
    'connection timeout expired',
    'connection failed: connection to server at "127.0.0.1", port 54328 failed: timeout expired',
])
def test_driver_connect_timeout_without_sqlstate_resumes_same_checkpoint(message):
    from backtest_data.ops.db_recovery import database_error_payload,AttemptResult,run_with_database_recovery
    diagnostic=database_error_payload(OperationalError('PRIVATE SQL',{'secret':'PRIVATE_KEY'},
        psycopg.OperationalError(message+' PRIVATE_KEY')))
    assert diagnostic['retryable'] is True and diagnostic['sqlstate'] is None
    assert 'PRIVATE_KEY' not in json.dumps(diagnostic)
    attempts=[]
    def execute(number,remaining):
        attempts.append(number)
        return AttemptResult(2,{'status':'blocked','reason':'OperationalError','database_error':diagnostic}) if number==1 else AttemptResult(0,{'status':'partial'})
    result=run_with_database_recovery(execute,retry_delays=(1,),sleep=lambda seconds:None)
    assert attempts==[1,2] and result.exit_code==0


@pytest.mark.parametrize('error',[
    psycopg.errors.QueryCanceled('canceling statement due to statement timeout'),
    psycopg.OperationalError('statement timeout expired'),
    psycopg.OperationalError('timeout expired'),
])
def test_query_or_unidentified_timeout_is_not_connection_retry(error):
    from backtest_data.ops.db_recovery import database_error_payload
    assert database_error_payload(OperationalError(None,None,error))['retryable'] is False


def test_statement_timeout_proof_is_separate_from_generic_database_retry():
    from backtest_data.ops.db_recovery import database_error_payload
    orig=Exception('PRIVATE SQL');orig.sqlstate='57014'
    orig.diag=SimpleNamespace(message_primary='canceling statement due to statement timeout')
    value=database_error_payload(OperationalError('PRIVATE SQL',{'key':'PRIVATE_KEY'},orig))
    assert value['statement_timeout'] is True and value['retryable'] is False
    assert 'PRIVATE' not in json.dumps(value)
    orig.diag=SimpleNamespace(message_primary='canceling statement due to user request')
    assert not database_error_payload(OperationalError(None,None,orig)).get('statement_timeout')


def transient():
    from backtest_data.ops.db_recovery import AttemptResult
    return AttemptResult(2,{'status':'blocked','reason':'OperationalError','database_error':{
        'category':'server_recovery','sqlstate':'57P03','retryable':True}})


def test_disconnected_worker_reuses_same_executor_with_preserved_attempt_history():
    from backtest_data.ops.db_recovery import AttemptResult,run_with_database_recovery
    attempts=[];events=[];waits=[]
    def execute(number,remaining):
        attempts.append((number,remaining))
        return transient() if number==1 else AttemptResult(0,{'status':'partial','coverage_complete':False})
    result=run_with_database_recovery(execute,retry_delays=(10,),sleep=waits.append,record=events.append)
    assert attempts==[(1,None),(2,None)] and waits==[10]
    assert result.exit_code==0 and events[0]['status']=='waiting_for_database'


@pytest.mark.parametrize('receipt',[
    None,{'status':'blocked','reason':'401'},
    {'status':'blocked','reason':'OperationalError'},
    {'status':'blocked','reason':'OperationalError','database_error':{'category':'permanent_or_unknown','retryable':True}},
    {'status':'partial','reason':'OperationalError','database_error':{'category':'connection_loss','retryable':True}},
    {'status':'blocked','reason':'OperationalError','database_error':{'category':'connection_loss','sqlstate':'28P01','retryable':True}},
    {'status':'blocked','reason':'OperationalError','database_error':{'category':'server_recovery','sqlstate':None,'retryable':True}},
])
def test_invalid_provider_or_normal_receipts_do_not_retry(receipt):
    from backtest_data.ops.db_recovery import AttemptResult,run_with_database_recovery
    attempts=[]
    result=run_with_database_recovery(lambda number,remaining:(attempts.append(number) or AttemptResult(2,receipt)),
        retry_delays=(10,),sleep=lambda seconds:pytest.fail('must not wait'))
    assert attempts==[1] and result.exit_code==2


def test_retry_limit_is_finite_and_reports_exhaustion():
    from backtest_data.ops.db_recovery import run_with_database_recovery
    attempts=[];events=[]
    run_with_database_recovery(lambda number,remaining:(attempts.append(number) or transient()),
        retry_delays=(1,2),sleep=lambda seconds:None,record=events.append)
    assert attempts==[1,2,3] and events[-1]['status']=='database_retry_exhausted'


def test_one_publication_budget_is_shared_across_retries_and_backoff():
    from backtest_data.ops.db_recovery import AttemptResult,run_with_database_recovery
    clock=[0.];remaining=[]
    def execute(number,budget):
        remaining.append(budget);clock[0]+=3
        return transient() if number==1 else AttemptResult(0,{'status':'budget_deferred'})
    result=run_with_database_recovery(execute,retry_delays=(4,),total_budget_seconds=10,
        monotonic=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    assert remaining==[10,3] and result.exit_code==0


@pytest.mark.parametrize('spent,delay',[(10,2),(8,4)])
def test_budget_exhaustion_during_attempt_or_backoff_does_not_spawn_again(spent,delay):
    from backtest_data.ops.db_recovery import run_with_database_recovery
    clock=[0.];attempts=[]
    def execute(number,remaining):
        attempts.append(number);clock[0]+=spent;return transient()
    result=run_with_database_recovery(execute,retry_delays=(delay,),total_budget_seconds=10,
        monotonic=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    assert attempts==[1] and result.receipt['status']=='budget_deferred'


def test_cli_emits_only_safe_database_diagnostics(monkeypatch,capsys):
    import backtest_data.cli as cli
    monkeypatch.setattr(cli.Settings,'from_env',lambda:SimpleNamespace(database_url='postgresql:///fake_test'))
    monkeypatch.setattr(cli,'make_engine',lambda url:SimpleNamespace(dispose=lambda:None))
    def fail(*args):
        raise OperationalError('private SQL',{'key':'PRIVATE_KEY'},psycopg.OperationalError('server closed the connection unexpectedly PRIVATE_KEY'))
    monkeypatch.setattr(cli,'_dispatch',fail)
    assert cli.main(['status'])==2
    output=capsys.readouterr().out
    assert 'PRIVATE_KEY' not in output and json.loads(output)['database_error']['retryable'] is True
