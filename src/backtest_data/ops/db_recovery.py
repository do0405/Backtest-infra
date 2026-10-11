"""Safe database failure classification and bounded checkpoint-worker recovery.

The executor owns subprocesses, logs and domain locks. This module neither
changes SQL work nor retries supplier responses or unclassified failures.
"""
from dataclasses import dataclass
import math
import re
import time

DEFAULT_RETRY_DELAYS=(10,30,60,120,120,120)
RECOVERY_STATES={'57P01','57P02','57P03'}


def database_error_payload(exc):
    from sqlalchemy.exc import OperationalError,InterfaceError,PendingRollbackError
    if isinstance(exc,PendingRollbackError) and getattr(exc,'code',None)=='8s2b':
        # SQLAlchemy's specific disconnected-Connection transaction gate;
        # failed SQL Session transactions use a different code and stay fatal.
        return {'category':'connection_loss','sqlstate':None,'connection_invalidated':True,'retryable':True}
    orig=getattr(exc,'orig',None)
    raw_code=getattr(orig,'sqlstate',None) or getattr(orig,'pgcode',None)
    code=raw_code if isinstance(raw_code,str) and re.fullmatch(r'[A-Z0-9]{5}',raw_code) else None
    invalidated=getattr(exc,'connection_invalidated',False) is True
    eligible=isinstance(exc,(OperationalError,InterfaceError))
    retryable=False
    if eligible and code is not None:
        retryable=code.startswith('08') or code in RECOVERY_STATES
    elif eligible and raw_code is None:
        # Some transport/startup failures have no SQLSTATE. Inspect only the
        # known driver error in memory; never publish its text or parameters.
        driver_error=type(orig).__name__ in {'OperationalError','InterfaceError'}
        message=str(orig).lower() if driver_error else ''
        known_loss=any(term in message for term in ('connection refused','server closed the connection unexpectedly',
            'connection is closed','connection not open','ssl connection has been closed',
            'could not receive data from server','could not send data to server','connection timed out',
            'the database system is starting up','the database system is not yet accepting connections',
            'the database system is in recovery mode','the database system is shutting down'))
        # libpq/psycopg connection establishment has no SQLSTATE on timeout.
        # Require connection wording so an unclassified query timeout stays fatal.
        connect_timeout=('connection timeout expired' in message or
            'connection to server' in message and 'timeout expired' in message)
        retryable=invalidated or known_loss or connect_timeout
    payload={'category':'server_recovery' if retryable and code in RECOVERY_STATES else
        'connection_loss' if retryable else 'permanent_or_unknown','sqlstate':code,
        'connection_invalidated':invalidated,'retryable':retryable}
    if eligible and code=='57014' and getattr(getattr(orig,'diag',None),'message_primary',None)=='canceling statement due to statement timeout':
        # Separate proof for the opt-in continuous controller. The existing
        # finite DB retry policy still excludes cancellation/query timeouts.
        payload['statement_timeout']=True
    return payload


@dataclass(frozen=True)
class AttemptResult:
    exit_code:int
    receipt:dict|None


def retryable_result(result):
    receipt=result.receipt
    if result.exit_code!=2 or not isinstance(receipt,dict):return False
    if receipt.get('status')!='blocked' or receipt.get('reason') not in {'OperationalError','InterfaceError','PendingRollbackError'}:return False
    diagnostic=receipt.get('database_error')
    if not isinstance(diagnostic,dict) or diagnostic.get('retryable') is not True:return False
    if receipt.get('reason')=='PendingRollbackError' and diagnostic.get('connection_invalidated') is not True:return False
    code=diagnostic.get('sqlstate')
    if diagnostic.get('category')=='server_recovery':return code in RECOVERY_STATES
    if diagnostic.get('category')=='connection_loss':
        return code is None or isinstance(code,str) and re.fullmatch(r'08[A-Z0-9]{3}',code) is not None
    return False


def run_with_database_recovery(execute,*,retry_delays=DEFAULT_RETRY_DELAYS,total_budget_seconds=None,
        sleep=time.sleep,monotonic=time.monotonic,record=lambda event:None):
    delays=tuple(retry_delays)
    if len(delays)>6 or any(type(delay) not in (int,float) or not math.isfinite(delay) or delay<=0 for delay in delays):
        raise ValueError('bounded_positive_database_retry_delays_required')
    if total_budget_seconds is not None and (type(total_budget_seconds) not in (int,float)
            or not math.isfinite(total_budget_seconds) or total_budget_seconds<=0):
        raise ValueError('positive_worker_budget_required')
    deadline=monotonic()+total_budget_seconds if total_budget_seconds is not None else None
    def remaining():return max(0.,deadline-monotonic()) if deadline is not None else None
    def deferred():
        record({'status':'budget_deferred','reason':'database_recovery_budget_exhausted'})
        return AttemptResult(0,{'status':'budget_deferred','reason':'database_recovery_budget_exhausted','coverage_complete':False})
    for attempt in range(1,len(delays)+2):
        budget=remaining()
        if budget is not None and budget<=0:return deferred()
        result=execute(attempt,budget)
        if not isinstance(result,AttemptResult):raise TypeError('worker_attempt_result_required')
        if not retryable_result(result):return result
        budget=remaining()
        if budget is not None and budget<=0:return deferred()
        if attempt>len(delays):
            record({'status':'database_retry_exhausted','attempt':attempt,'reason':'database_retry_limit_reached'})
            return result
        delay=min(delays[attempt-1],budget) if budget is not None else delays[attempt-1]
        record({'status':'waiting_for_database','attempt':attempt,'retry_in_seconds':delay})
        sleep(delay)
    raise AssertionError('unreachable_recovery_state')
