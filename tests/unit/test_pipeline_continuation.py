import json


def test_only_explicit_successful_stages_are_reused(tmp_path):
    from backtest_data.ops.pipeline_continuation import completed_stage
    runtime=tmp_path/'runtime.json';output=tmp_path/'output.json'
    stage={'runtime':str(runtime),'stdout':str(output)}
    runtime.write_text(json.dumps({'exit_code':0,'finished_at':'date'}))
    output.write_text(json.dumps({'status':'audit_complete','coverage_complete':False}))
    assert completed_stage('coverage',stage) is True
    output.write_text(json.dumps({'status':'budget_deferred'}))
    assert completed_stage('coverage',stage) is False
    output.write_text(json.dumps({'status':'audit_complete'}))
    runtime.write_text(json.dumps({'exit_code':2,'finished_at':'date'}))
    assert completed_stage('coverage',stage) is False


def test_accepted_sec_access_gap_is_history_not_retried_on_unrelated_stage_failure(tmp_path):
    from backtest_data.ops.pipeline_continuation import completed_stage
    runtime=tmp_path/'runtime.json';output=tmp_path/'output.json'
    runtime.write_text(json.dumps({'exit_code':2,'finished_at':'date'}))
    output.write_text(json.dumps({'status':'blocked','reason':'sec_source_HTTP_403'}))
    assert completed_stage('sec_metadata',{'runtime':str(runtime),'stdout':str(output)})
