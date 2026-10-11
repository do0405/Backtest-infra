"""Lifecycle decisions only: no supplier, lease, checkpoint or data mutation."""
from dataclasses import dataclass

from .db_recovery import retryable_result

CORE_ROLES={'prices','fundamentals','alpaca_prices','krx_quotes'}
TEMPORARY={'lease_lost','800','020','900','429','500','502','503','504','HTTP_429',
    'HTTP_500','HTTP_502','HTTP_503','HTTP_504','rate_budget_deferred','retry_deferred',
    'transport_failed','transport_error','EGW00201','EGW00133','free_history_range_deferred'}
AUTH={'401','403','HTTP_401','HTTP_403','010','011','012','requires_credentials',
    'requires_credentials_for_free_historical_sip','requires_identifying_user_agent','token_unavailable'}
FINISHED={'public_pit':{'audit_pass_complete'},'daily_publication':{'source_pass_complete'},
    'supplier_prices':{'request_queue_complete'},'vwap_inputs':{'request_traversal_complete'},
    'clean_completion':{'runnable_pass_complete'},'metric_evaluation':{'evaluation_complete'}}


@dataclass(frozen=True)
class Decision:
    action:str
    reason:str
    delay_seconds:int=0
    requires_user:bool=False


def decide(role,result,*,runnable=None,failure_streak=0,dependency_active=False,nested_result=None,storage_ready=False):
    value=result.receipt
    delay=min(3600,60*2**min(max(0,failure_streak),6))
    if not isinstance(value,dict):return Decision('action_required','result_unconfirmed',requires_user=True)
    status=value.get('status');reason=value.get('reason')
    if status=='disk_floor_deferred' or reason=='E_free_space_guard':
        if storage_ready:return Decision('resume','storage_guard_released',5)
        return Decision('action_required','storage_space_required',requires_user=True)
    if result.exit_code==0 and status=='budget_deferred':return Decision('resume','batch_budget_finished',5)
    if role=='metric_evaluation' and reason=='input_pipeline_not_successful' and dependency_active:
        return Decision('wait','waiting_for_financial_input_audit',60)
    if role=='clean_completion' and reason=='enrichment_stage_failed' and nested_result:
        return decide(role,nested_result,runnable=runnable,failure_streak=failure_streak)
    if retryable_result(result):return Decision('resume','temporary_database_connection',delay)
    diagnostic=value.get('database_error',{})
    if diagnostic.get('sqlstate') in {'28P01','42501'}:
        return Decision('action_required','authentication_or_permission_required',requires_user=True)
    if (result.exit_code==2 and reason in {'OperationalError','InterfaceError'} and
        diagnostic.get('sqlstate')=='57014' and diagnostic.get('statement_timeout') is True):
        return Decision('resume','temporary_statement_timeout',delay)
    if result.exit_code==0 and status in FINISHED.get(role,set()):return Decision('complete','declared_pass_finished')
    if status in {'dependency_wait','already_running','duplicate_prevented'}:
        return Decision('wait','existing_owner_or_dependency',60)
    reasons=list(value.get('blocked_sources',{}).values())
    stages=value.get('blocked_stages',[])
    if isinstance(stages,list):reasons += [s.get('reason') for s in stages if isinstance(s,dict)]
    if status=='blocked' and reason:reasons.append(reason)
    if reasons:
        if all(r in TEMPORARY for r in reasons):
            if any(r in {'800','020','900'} for r in reasons):delay=max(1800,delay)
            return Decision('resume','temporary_source_or_lease',delay)
        if any(r in AUTH for r in reasons):return Decision('action_required','authentication_or_permission_required',requires_user=True)
        return Decision('action_required','source_or_program_error',requires_user=True)
    if result.exit_code==0 and role in CORE_ROLES and status=='partial':
        if runnable is True:return Decision('resume','planned_queue_remaining',5)
        if runnable is False:return Decision('complete','runnable_queue_exhausted')
        return Decision('wait','queue_state_unconfirmed',60)
    return Decision('action_required','source_or_program_error',requires_user=True)
