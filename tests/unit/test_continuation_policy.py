import pytest

from backtest_data.ops.db_recovery import AttemptResult


def choose(receipt,code=0,role='prices',**kwargs):
    from backtest_data.ops.continuation_policy import decide
    return decide(role,AttemptResult(code,receipt),**kwargs)


def test_budget_is_a_batch_boundary_without_overall_retry_limit():
    for cycle in range(20):
        result=choose({'status':'budget_deferred'},failure_streak=cycle)
        assert result.action=='resume' and not result.requires_user


@pytest.mark.parametrize('reason',['lease_lost','800','020','900','429','503','HTTP_429','HTTP_504',
    'rate_budget_deferred','retry_deferred','transport_failed','transport_error','EGW00201','EGW00133'])
def test_declared_temporary_sources_wait_and_resume(reason):
    d=choose({'status':'partial','blocked_sources':{'opendart':reason},'blocked_stages':[]})
    assert d.action=='resume' and d.delay_seconds>=60 and not d.requires_user


def test_mixed_auth_and_temporary_block_is_not_an_unlimited_auth_retry():
    d=choose({'status':'partial','blocked_sources':{'sec':'403'},'blocked_stages':[{'reason':'lease_lost'}]})
    assert d.action=='action_required' and d.requires_user


def test_known_database_disconnect_continues_after_old_retry_exhaustion():
    d=choose({'status':'blocked','reason':'OperationalError','database_error':{
        'category':'connection_loss','sqlstate':'08006','retryable':True}},2,failure_streak=30)
    assert d.action=='resume' and d.delay_seconds==3600


def test_only_identified_statement_timeout_is_transient():
    v={'status':'blocked','reason':'OperationalError','database_error':{'sqlstate':'57014','retryable':False}}
    assert choose(v,2).requires_user
    v['database_error']['statement_timeout']=True
    assert choose(v,2).action=='resume'


@pytest.mark.parametrize('code',['28P01','42501','42601','53200'])
def test_auth_schema_and_resource_errors_require_action(code):
    assert choose({'status':'blocked','reason':'OperationalError','database_error':{
        'category':'permanent_or_unknown','sqlstate':code,'retryable':False}},2).requires_user


def test_partial_requires_real_queue_exhaustion_evidence():
    receipt={'status':'partial','blocked_sources':{},'blocked_stages':[]}
    assert choose(receipt,runnable=True).action=='resume'
    assert choose(receipt,runnable=False).action=='complete'
    assert choose(receipt,runnable=None).action=='wait'


@pytest.mark.parametrize('role,status',[
    ('public_pit','audit_pass_complete'),('daily_publication','source_pass_complete'),
    ('supplier_prices','request_queue_complete'),('vwap_inputs','request_traversal_complete'),
    ('clean_completion','runnable_pass_complete'),('metric_evaluation','evaluation_complete'),
])
def test_finite_declared_pass_completion_is_not_recollected(role,status):
    d=choose({'status':status,'coverage_complete':False},role=role)
    assert d.action=='complete' and not d.requires_user


def test_mislabelled_completion_unknown_receipt_and_disk_guard_are_not_success():
    assert choose({'status':'evaluation_complete'},role='prices').requires_user
    assert choose(None,code=3).requires_user
    assert choose({'status':'disk_floor_deferred'},role='vwap_inputs').requires_user
    assert choose({'status':'blocked','reason':'E_free_space_guard'},2,role='supplemental_sources').requires_user


def test_metric_wait_and_nested_transient_pipeline_failure_continue():
    assert choose({'status':'blocked','reason':'input_pipeline_not_successful'},2,
        role='metric_evaluation',dependency_active=True).action=='wait'
    nested=AttemptResult(2,{'status':'blocked','reason':'OperationalError','database_error':{
        'category':'server_recovery','sqlstate':'57P03','retryable':True}})
    assert choose({'status':'blocked','reason':'enrichment_stage_failed'},2,
        role='clean_completion',nested_result=nested).action=='resume'


def test_cooldown_is_capped_without_disabling_recovery():
    assert choose({'status':'budget_deferred'},failure_streak=999).delay_seconds==5
    d=choose({'status':'partial','blocked_sources':{'opendart':'800'}},failure_streak=999)
    assert d.action=='resume' and d.delay_seconds==3600


def test_cleared_disk_guard_resumes_without_checkpoint_reset():
    assert choose({'status':'disk_floor_deferred'},role='vwap_inputs',storage_ready=True).action=='resume'
