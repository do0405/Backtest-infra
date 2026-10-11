from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
import json
from pathlib import Path

import pytest


NOW = datetime(2026, 10, 11, 13, 0, tzinfo=timezone.utc)
REQUEST = "f42e26f1-3fd8-489d-b819-85d8a16f304e"


def module():
    try:
        return importlib.import_module("backtest_data.ops.remote_schedule")
    except ModuleNotFoundError:
        pytest.fail("Missing durable remote schedule recovery policy")


def run(identity=123, *, status="completed", conclusion="success", age=4, title="batch"):
    return {"id": identity, "status": status, "conclusion": conclusion,
            "created_at": (NOW-timedelta(hours=age)).isoformat(), "display_title": title}


def snapshot(runs=None):
    return {"enabled": True, "workflow_active": True, "runs": [run()] if runs is None else runs}


def cycle(state=None, view=None, dispatch=None, persisted=None, **kwargs):
    state = {} if state is None else state
    saved = [] if persisted is None else persisted
    calls = []
    def send(identity, budget):
        calls.append((identity, budget))
        if dispatch:
            dispatch(identity, budget)
    result=module().advance(state, snapshot() if view is None else view, send,
                            lambda value:saved.append(json.loads(json.dumps(value))),
                            now=NOW, request_id=REQUEST, **kwargs)
    return result,calls,saved


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "pending", "requested"])
def test_existing_active_or_waiting_run_prevents_dispatch(status):
    state,calls,_=cycle(view=snapshot([run(status=status,conclusion=None)]))
    assert calls==[] and state["status"]=="waiting_for_existing_remote_run"


def test_disabled_pipeline_is_never_enabled_by_watchdog():
    view=snapshot();view["enabled"]=False
    state,calls,_=cycle(view=view)
    assert calls==[] and state["status"]=="pipeline_disabled"


def test_recent_run_respects_two_hours_plus_grace():
    state,calls,_=cycle(view=snapshot([run(age=2)]))
    assert calls==[] and state["status"]=="waiting_for_schedule_window"


def test_overdue_dispatch_has_durable_intent_before_request():
    saved=[]
    def send(identity,budget):
        assert saved[-1]["intent"]["request_id"]==identity==REQUEST
        assert saved[-1]["intent"]["status"]=="prepared"
        assert budget==300
    state,calls,_=cycle(dispatch=send,persisted=saved,budget_seconds=300)
    assert calls==[(REQUEST,300)] and state["intent"]["status"]=="accepted"


def test_accepted_request_is_not_dispatched_again_after_restart():
    first,_,_=cycle()
    restored=json.loads(json.dumps(first))
    state,calls,_=cycle(restored)
    assert calls==[] and state["status"]=="waiting_for_dispatch_receipt"


def test_response_lost_after_acceptance_is_reconciled_without_duplicate():
    def lost(*_):raise TimeoutError("synthetic secret must never be retained")
    first,calls,_=cycle(dispatch=lost)
    assert len(calls)==1 and first["intent"]["status"]=="uncertain"
    matching=run(identity=456,status="in_progress",conclusion=None,
                 title="KRX checkpoint continuation "+REQUEST,age=0)
    second,calls,_=cycle(first,snapshot([matching]))
    assert calls==[] and second["intent"]["observed_run_id"]==456
    assert second["status"]=="waiting_for_existing_remote_run"
    assert "synthetic secret" not in json.dumps(second)


def test_unobserved_ambiguous_dispatch_never_blindly_retries():
    first,_,_=cycle(dispatch=lambda *_:(_ for _ in ()).throw(TimeoutError()))
    first["intent"]["created_at"]=(NOW-timedelta(hours=5)).isoformat()
    state,calls,_=cycle(first)
    assert calls==[] and state["status"]=="launch_uncertain" and state["requires_user"]


def test_dispatch_permission_rejection_is_latched_across_polls_and_restart():
    def forbidden(*_):raise ValueError("github_access_requires_action")
    first,calls,_=cycle(dispatch=forbidden)
    assert len(calls)==1 and first["intent"]["status"]=="rejected"
    restored=json.loads(json.dumps(first))
    for _ in range(3):
        restored,calls,_=cycle(restored,dispatch=forbidden)
        assert calls==[] and restored["requires_user"]
        assert restored["status"]=="github_access_requires_action"


def test_completed_intent_can_be_followed_by_next_overdue_batch():
    first,_,_=cycle()
    first["intent"]["created_at"]=(NOW-timedelta(hours=5)).isoformat()
    matching=run(identity=456,title="KRX checkpoint continuation "+REQUEST)
    next_id="91d81b28-e9ab-4c11-8b4d-d7e78fe6dc47"
    calls=[];saved=[]
    state=module().advance(first,snapshot([matching]),lambda *v:calls.append(v),
                          lambda v:saved.append(json.loads(json.dumps(v))),
                          now=NOW,request_id=next_id)
    assert calls==[(next_id,6000)]
    assert state["previous_intent"]["observed_run_id"]==456


def test_unclassified_failed_run_fails_closed():
    state,calls,_=cycle(view=snapshot([run(conclusion="failure")]))
    assert calls==[] and state["status"]=="remote_failure_requires_review" and state["requires_user"]


def test_unknown_status_or_missing_clock_does_not_start_a_collector():
    with pytest.raises(ValueError):cycle(view=snapshot([run(status="unknown")]))
    invalid=run();invalid.pop("created_at")
    with pytest.raises(ValueError):cycle(view=snapshot([invalid]))


def test_timed_out_batch_can_resume_saved_checkpoint():
    state,calls,_=cycle(view=snapshot([run(conclusion="timed_out")]))
    assert len(calls)==1 and state["intent"]["status"]=="accepted"


def test_input_validation_rejects_unsafe_request_and_budget():
    for budget in (0,6001,True):
        with pytest.raises(ValueError):cycle(budget_seconds=budget)
    with pytest.raises(ValueError):
        module().advance({},snapshot(),lambda *a:None,lambda s:None,now=NOW,request_id="bad\nvalue")


def client_snapshot(monkeypatch, *, cancel="false", disabled=False, old_active=None):
    value=module()
    text=("name: Remote data update\nrun-name: "+value.TITLE_PREFIX+
          "${{ inputs.continuation_request_id || github.run_number }}\n"
          "on:\n  workflow_dispatch:\n    inputs:\n      continuation_request_id: {}\n"
          "concurrency:\n  group: "+value.CONCURRENCY_GROUP+"\n  cancel-in-progress: "+cancel+"\n")
    import base64
    responses={"":{"default_branch":"main","disabled":False,"archived":False,"fork":False},
        "actions/permissions":{"enabled":True},
        "actions/variables?per_page=100":{"variables":[{"name":k,"value":"false" if disabled else "true"} for k in value.FLAGS]},
        "actions/workflows/"+value.WORKFLOW:{"state":"active"},
        "contents/.github/workflows/"+value.WORKFLOW:{"content":base64.b64encode(text.encode()).decode()},
        "actions/workflows/"+value.WORKFLOW+"/runs?per_page=100":{"workflow_runs":[run()]}}
    for status in value.ACTIVE:
        responses["actions/workflows/"+value.WORKFLOW+"/runs?per_page=100&status="+status]={"workflow_runs":[old_active] if old_active and status==old_active["status"] else []}
    client=value.GitHubSchedule("gh","owner/repo")
    monkeypatch.setattr(client,"api",lambda suffix:responses[suffix])
    return client.snapshot()


def test_cli_snapshot_inspects_active_runs_outside_recent_history(monkeypatch):
    old=run(identity=987,status="queued",conclusion=None,age=120)
    state,calls,_=cycle(view=client_snapshot(monkeypatch,old_active=old))
    assert calls==[] and state["status"]=="waiting_for_existing_remote_run"


def test_cli_snapshot_preserves_operator_disabled_gate(monkeypatch):
    state,calls,_=cycle(view=client_snapshot(monkeypatch,disabled=True))
    assert calls==[] and state["status"]=="pipeline_disabled"


def test_cli_snapshot_refuses_changed_concurrency_fence(monkeypatch):
    with pytest.raises(ValueError,match="fence"):
        client_snapshot(monkeypatch,cancel="true")


def test_repository_read_uses_canonical_endpoint_without_trailing_slash(monkeypatch):
    value=module();seen=[]
    def request(gh,args,**kwargs):
        seen.append(args)
        if args!=["api","repos/owner/repo"]:
            raise RuntimeError("repository_endpoint_not_found")
        return b'{"default_branch":"main"}'
    monkeypatch.setattr(value,"github",request)
    assert value.GitHubSchedule("gh","owner/repo").api("")=={"default_branch":"main"}
    assert seen==[["api","repos/owner/repo"]]


def host():
    path=Path(__file__).parents[2]/"scripts/remote_schedule_watchdog.py"
    spec=importlib.util.spec_from_file_location("watchdog_host",path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


def test_os_singleton_prevents_second_watchdog_until_lock_released(tmp_path):
    value=host();path=tmp_path/"watchdog.lock"
    first=value.singleton(path)
    try:
        with pytest.raises(RuntimeError,match="duplicate"):
            value.singleton(path)
    finally:first.close()
    second=value.singleton(path);second.close()


def test_original_handoff_and_manager_fence_required_without_credential_reads(tmp_path,monkeypatch):
    value=host();directory=tmp_path/"artifacts";directory.mkdir()
    cli=tmp_path/"cli";cli.mkdir()
    config={"enabled":True,"owner_id":"test-stream","identity_handoff_ready":True,
            "handoff_id":"original-handoff","manifest_id":"original-manifest",
            "github_config_directory":str(cli),"provider_slots":[{"source":"krx","account_id":"opaque",
            "owner":"remote","local_enabled":False,"market":"kr","domain":"prices"}]}
    handoff=tmp_path/"handoff.json";manager=tmp_path/"manager.json"
    handoff.write_text(json.dumps({"owner":"remote","local_enabled":False,"local_manager_fenced":True,
        "operator_stop":True,"status":"remote_account_activated","handoff_id":"original-handoff",
        "stream_id":"test-stream","manifest_id":"original-manifest"}))
    manager.write_text(json.dumps({"roles":{"krx_quotes":{"status":"operator_stopped"}}}))
    (directory/"kr-full-workers.json").write_text(json.dumps({"auxiliary":{
        "krx_quotes":{"remote_handoff_path":str(handoff)},"continuation_manager":{"runtime":str(manager)}}}))
    monkeypatch.delenv("GH_CONFIG_DIR",raising=False)
    value.verified_configuration(tmp_path,config)
    assert list(cli.iterdir())==[]
    manager.write_text(json.dumps({"roles":{"krx_quotes":{"status":"adopted_running"}}}))
    with pytest.raises(ValueError,match="fence"):
        value.verified_configuration(tmp_path,config)
