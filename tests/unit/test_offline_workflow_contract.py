from pathlib import Path
import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_pending_fallback_is_bounded_and_health_reports_deferred_truthfully():
    workflow = yaml.safe_load((ROOT/'.github/workflows/offline-core-refresh.yml').read_text())
    refresh = workflow['jobs']['refresh']
    assert refresh['permissions'] == {'contents':'write', 'actions':'read'}
    capture = next(step for step in refresh['steps'] if step.get('id') == 'capture')
    assert capture['run'] == 'python scripts/offline_runtime_launcher.py'
    saved = next(step for step in refresh['steps'] if 'upload-artifact@' in step.get('uses',''))
    assert 'always()' in saved['if'] and 'deferred' in saved['if']
    assert saved['with']['path'].endswith('/capture/pending.zip')
    assert saved['with']['retention-days'] == 90
    assert 'pending_sha256' in saved['with']['name'] and 'github.run_id' in saved['with']['name']
    outcome = workflow['jobs']['maintain']['with']['collection_outcome']
    assert 'publication_state' in outcome and "'deferred'" in outcome


def test_recovery_checks_are_required_on_relevant_main_pushes_without_database():
    workflow = yaml.safe_load((ROOT/'.github/workflows/offline-publication-ci.yml').read_text())
    triggers = workflow.get('on', workflow.get(True))
    assert triggers['push']['branches'] == ['main']
    assert 'tests/runtime/**' in triggers['push']['paths']
    job = workflow['jobs']['verify']
    assert 'services' not in job
    assert any('tests/runtime' in step.get('run','') for step in job['steps'])
    assert workflow['permissions'] == {'contents':'read'}


def test_production_extra_dispatch_has_no_historical_audit():
    workflow = yaml.safe_load((ROOT/'.github/workflows/remote-extended-refresh.yml').read_text())
    triggers = workflow.get('on', workflow.get(True))
    assert set(triggers['workflow_dispatch']['inputs']) == {'budget_seconds'}
    assert 'EXTRA_AUDIT_PREVIOUS_SEQUENCE' not in workflow['jobs']['refresh']['steps'][-1]['env']
    assert 'def audit_previous_scope(' not in (ROOT/'scripts/extra-task-archive-v9.patch').read_text()


def test_core_keeps_fallback_on_postpublication_attention():
    workflow = yaml.safe_load((ROOT/'.github/workflows/offline-core-refresh.yml').read_text())
    refresh = workflow['jobs']['refresh']
    saved = next(step for step in refresh['steps'] if 'upload-artifact@' in step.get('uses', ''))
    assert {'publication_state', 'source_index', 'pending_cleanup'} == set(refresh['outputs'])
    assert all(f"steps.capture.outputs.{phase} == 'deferred'" in saved['if']
               for phase in ('source_index', 'pending_cleanup'))
    maintenance = workflow['jobs']['maintain']
    assert maintenance['uses'] == './.github/workflows/collection-maintenance.yml'
    assert 'committed_with_warnings' in maintenance['with']['collection_outcome']
    observer = yaml.safe_load((ROOT/'.github/workflows/collection-maintenance.yml').read_text())
    job = observer['jobs']['maintain']
    assert job['permissions']['actions'] == 'read'
    assert 'EXTRA_COLLECTION_ALLOWED' in job['steps'][-1]['env']



def test_core_is_independent_and_dispatch_has_only_collection_budget():
    workflow = yaml.safe_load((ROOT/'.github/workflows/offline-core-refresh.yml').read_text())
    triggers = workflow.get('on', workflow.get(True))
    assert set(triggers) == {'schedule', 'workflow_dispatch'}
    assert set(triggers['workflow_dispatch']['inputs']) == {'budget_seconds'}
    refresh = workflow['jobs']['refresh']
    assert "github.ref == 'refs/heads/main'" in refresh['if']
    assert 'ref' not in refresh['steps'][0].get('with', {})
