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
    outcome = workflow['jobs']['maintain']['steps'][-1]['env']['COLLECTION_OUTCOME']
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
