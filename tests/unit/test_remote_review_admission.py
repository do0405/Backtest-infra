"""Only an exact reviewed timeout may pass the existing continuation gates."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import copy
import json
import pytest
from backtest_data.ops import remote_schedule as policy

NOW = datetime(2026, 10, 12, 9, tzinfo=timezone.utc)
REQUEST = 'c72ad896-4ff4-40c5-a344-b315a311ca8d'
FILES = ['src/backtest_data/remote/codec.py', 'src/backtest_data/remote/bundle.py',
    'src/backtest_data/remote/folder.py', 'src/backtest_data/remote/worker.py',
    'src/backtest_data/remote/source_handlers.py', 'src/backtest_data/prices/krx_quote_collection.py',
    'src/backtest_data/ingestion/http.py', 'scripts/remote_release_cycle.py',
    '.github/workflows/remote-data-update.yml']

def view():
    binding = {'owner_id': 'krx-live-fc021362', 'handoff_id': '1141be31-6e1b-4eb2-a9fb-3a74e6baf019',
        'manifest_id': 'de0c06cb-6279-4df6-86a8-6b6ab226d6e0'}
    proof = {'version': 1, 'repository': 'do0405/Backtest-infra', 'workflow': policy.WORKFLOW,
        **binding, 'failed_run_id': 38196650268, 'failed_run_attempt': 1, 'failed_head_sha': 'a'*40,
        'failed_conclusion': 'cancelled', 'failure_reason': 'verified_job_timeout_before_release',
        'cause_proof_sha256': 'b'*64, 'repair_sha': 'c'*40, 'continuation_sha': 'e'*40,
        'continuation_ref': 'krx-reviewed-38196650268-a1-'+'e'*12,
        'repair_files': {path: 'd'*40 for path in FILES}}
    return {'enabled': True, 'workflow_active': True, 'repository': proof['repository'],
        'workflow': policy.WORKFLOW, 'ownership_binding': binding,
        'runs': [{'id': proof['failed_run_id'], 'run_attempt': 1, 'head_sha': 'a'*40,
            'status': 'completed', 'conclusion': 'cancelled',
            'created_at': (NOW-timedelta(hours=7)).isoformat(), 'display_title': 'native'}],
        'reviewed_failure': proof, 'cause_proof_verified': True, 'current_head_sha': 'e'*40,
        'repair_ancestry_status': 'ahead', 'current_repair_blobs': dict(proof['repair_files'])}

def cycle(data, state=None):
    calls, saved = [], []
    def persist(value): saved.append(copy.deepcopy(value))
    def dispatch(*args):
        assert saved[-1]['intent']['status'] == 'prepared'
        assert saved[-1]['intent']['reviewed_failure_sha256']
        calls.append(args)
    result = policy.advance({} if state is None else state, data, dispatch, persist,
        now=NOW, request_id=REQUEST)
    return result, calls, saved

def test_exact_reviewed_timeout_uses_original_durable_dispatch_path():
    result, calls, saved = cycle(view())
    assert calls == [(REQUEST, 6000)]
    assert result['intent']['status'] == 'accepted'
    assert result['intent']['reviewed_failed_run_id'] == 38196650268
    assert result['intent']['continuation_ref'] == view()['reviewed_failure']['continuation_ref']
    assert result['intent']['continuation_sha'] == 'e'*40
    assert saved[-2]['intent']['reviewed_failure_sha256'] == result['intent']['reviewed_failure_sha256']

def test_review_use_and_uuid_are_committed_together_before_network():
    result, _, saved = cycle(view())
    digest = result['intent']['reviewed_failure_sha256']
    assert saved[0]['reviewed_failure_uses'][digest] == REQUEST
    assert saved[0]['intent']['request_id'] == REQUEST
    _, calls, _ = cycle(view(), copy.deepcopy(result))
    assert calls == []

@pytest.mark.parametrize('field,value', [('failed_run_id', 5), ('failed_run_attempt', 2),
    ('failed_head_sha', 'f'*40), ('repository', 'other/repo'), ('workflow', 'other.yml'),
    ('owner_id', 'other-owner'), ('handoff_id', '4eedbb6d-8187-4981-b35e-a6c882cfdd96'),
    ('manifest_id', 'ba4bd5c0-cd99-4c9d-a0a4-94dd13cf6da6')])
def test_changed_failure_or_handoff_never_consumes_review(field, value):
    data = view(); data['reviewed_failure'][field] = value
    result, calls, _ = cycle(data)
    assert calls == [] and result['status'] == 'remote_failure_requires_review'
    assert not result.get('reviewed_failure_uses')

@pytest.mark.parametrize('field,value', [('repair_ancestry_status', 'diverged'),
    ('current_head_sha', None), ('current_repair_blobs', {})])
def test_ancestry_alone_or_missing_blob_readback_is_not_repair_proof(field, value):
    data = view(); data[field] = value
    result, calls, _ = cycle(data)
    assert calls == [] and result['requires_user'] is True

def test_retained_hash_mismatch_never_retries_cancellation():
    data = view(); data['current_repair_blobs'][FILES[0]] = 'f'*40
    result, calls, _ = cycle(data)
    assert calls == [] and result['status'] == 'remote_failure_requires_review'

def test_active_queue_recent_window_and_uncertain_intent_still_win():
    data = view(); data['runs'].append({**data['runs'][0], 'id': 8, 'status': 'queued',
        'created_at': NOW.isoformat(), 'conclusion': None})
    result, calls, _ = cycle(data)
    assert calls == [] and result['status'] == 'waiting_for_existing_remote_run'
    data = view(); data['runs'][0]['created_at'] = (NOW-timedelta(hours=1)).isoformat()
    result, calls, _ = cycle(data)
    assert calls == [] and result['status'] == 'waiting_for_schedule_window'
    state = {'intent': {'request_id': REQUEST, 'status': 'uncertain',
        'created_at': (NOW-timedelta(minutes=20)).isoformat()}}
    result, calls, _ = cycle(view(), state)
    assert calls == [] and result['status'] == 'launch_uncertain'

def test_no_review_retains_existing_cancelled_failure_hold():
    data = view(); data.pop('reviewed_failure')
    result, calls, _ = cycle(data)
    assert calls == [] and result['status'] == 'remote_failure_requires_review'

def test_review_requires_local_cause_verification_and_canonical_ref():
    for change in ('cause', 'ref'):
        data = view()
        if change == 'cause': data['cause_proof_verified'] = False
        else: data['reviewed_failure']['continuation_ref'] = 'main'
        result, calls, _ = cycle(data)
        assert calls == [] and result['status'] == 'remote_failure_requires_review'

def test_used_review_cannot_be_consumed_after_observed_intent_again():
    data = view(); digest = policy.review_digest(data['reviewed_failure'])
    old = '6dff4e53-8d94-4a7b-ac23-7578aef4c567'
    state = {'reviewed_failure_uses': {digest: old},
             'intent': {'request_id': old, 'status': 'observed', 'created_at': NOW.isoformat()}}
    result, calls, _ = cycle(data, state)
    assert calls == [] and result['status'] == 'remote_failure_requires_review'

def test_matching_uuid_with_wrong_executed_code_is_held():
    data = view()
    data['runs'][0]['display_title'] = policy.TITLE_PREFIX + REQUEST
    state = {'intent': {'request_id': REQUEST, 'status': 'accepted',
        'created_at': NOW.isoformat(), 'continuation_sha': 'e'*40}}
    result, calls, _ = cycle(data, state)
    assert calls == [] and result['status'] == 'dispatch_code_identity_requires_review'

def test_unknown_or_incomplete_review_schema_is_not_permission():
    for field, value in [('failed_run_id', True), ('version', 2), ('repair_files', {}),
                         ('cause_proof_sha256', 'bad'), ('repair_sha', '../main')]:
        data = view(); data['reviewed_failure'][field] = value
        result, calls, _ = cycle(data)
        assert calls == [] and result['status'] == 'remote_failure_requires_review'

def client(monkeypatch, *, tag_sha='e'*40, blob_sha='d'*40, cancel='false'):
    import base64
    from pathlib import Path
    text = (Path(__file__).parents[2]/'.github/workflows/remote-data-update.yml').read_text()
    main_doc = {'content': base64.b64encode(text.encode()).decode()}
    tagged = {'content': base64.b64encode(text.replace('cancel-in-progress: false',
        'cancel-in-progress: '+cancel).encode()).decode()}
    proof = view()['reviewed_failure']
    responses = {'': {'default_branch': 'main'}, 'actions/permissions': {'enabled': True},
        'actions/variables?per_page=100': {'variables': [{'name': k, 'value': 'true'} for k in policy.FLAGS]},
        'actions/workflows/'+policy.WORKFLOW: {'state': 'active'},
        'contents/.github/workflows/'+policy.WORKFLOW: main_doc,
        'actions/workflows/'+policy.WORKFLOW+'/runs?per_page=100': {'workflow_runs': view()['runs']},
        'git/ref/tags/'+proof['continuation_ref']: {'object': {'type': 'commit', 'sha': tag_sha}},
        'compare/'+proof['repair_sha']+'...'+proof['continuation_sha']: {'status': 'ahead'},
        'git/trees/'+proof['continuation_sha']+'?recursive=1': {'truncated': False,
            'tree': [{'type': 'blob', 'path': path, 'sha': blob_sha} for path in FILES]},
        'contents/.github/workflows/'+policy.WORKFLOW+'?ref='+proof['continuation_sha']: tagged}
    for status in policy.ACTIVE:
        responses['actions/workflows/'+policy.WORKFLOW+'/runs?per_page=100&status='+status] = {'workflow_runs': []}
    value = policy.GitHubSchedule('gh', proof['repository'])
    monkeypatch.setattr(value, 'api', lambda suffix: responses[suffix])
    return value

def test_snapshot_verifies_exact_tag_tree_ancestry_and_dispatch_ref(monkeypatch):
    value = client(monkeypatch); data = view(); proof = data['reviewed_failure']
    observed = value.snapshot(reviewed_failure=proof, ownership_binding=data['ownership_binding'],
                              cause_proof_verified=True)
    assert policy._review_for_run(observed, observed['runs'][0])
    seen = []
    monkeypatch.setattr(policy, 'github', lambda gh,args,**kw: seen.append(args))
    value.dispatch(REQUEST,6000,ref=proof['continuation_ref'])
    assert seen[0][seen[0].index('--ref')+1] == proof['continuation_ref']
    with pytest.raises(ValueError,match='reviewed_dispatch_ref'):
        value.dispatch(REQUEST,6000,ref='unverified-tag')

@pytest.mark.parametrize('option', [{'tag_sha': 'f'*40}, {'blob_sha': 'f'*40}])
def test_changed_tag_or_tree_cannot_fall_back_to_main(monkeypatch, option):
    value = client(monkeypatch, **option); data = view()
    observed = value.snapshot(reviewed_failure=data['reviewed_failure'],
        ownership_binding=data['ownership_binding'], cause_proof_verified=True)
    result, calls, _ = cycle(observed)
    assert calls == [] and result['status'] == 'remote_failure_requires_review'
    assert value.reviewed_ref is None

def test_tagged_workflow_serial_fence_verified_at_tag_target(monkeypatch):
    value = client(monkeypatch,cancel='true'); data=view()
    with pytest.raises(ValueError,match='fence'):
        value.snapshot(reviewed_failure=data['reviewed_failure'],
            ownership_binding=data['ownership_binding'],cause_proof_verified=True)

def test_host_review_requires_original_timeout_cause_bytes(tmp_path):
    import importlib.util
    from pathlib import Path
    target=Path(__file__).parents[2]/'scripts/remote_schedule_watchdog.py'
    spec=importlib.util.spec_from_file_location('reviewed_watchdog',target)
    host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host)
    root=tmp_path/'watchdog';root.mkdir();area=tmp_path/'artifacts';area.mkdir()
    proof=view()['reviewed_failure'];proof['cause_proof_path']='artifacts/cause.json'
    cause={'timeout_annotation_confirmed':True,'last_confirmed_phase':{'name':'archive_prepared'},
        'run': {'id':proof['failed_run_id'],'run_attempt':1,'head_sha':'a'*40,'conclusion':'cancelled'}}
    data=json.dumps(cause).encode();(area/'cause.json').write_bytes(data)
    proof['cause_proof_sha256']=sha256(data).hexdigest()
    (root/'reviewed-failure.json').write_text(json.dumps(proof))
    assert host.reviewed_failure(root,tmp_path)==proof
    (area/'cause.json').write_bytes(data+b' ')
    with pytest.raises(ValueError,match='hash'):
        host.reviewed_failure(root,tmp_path)
