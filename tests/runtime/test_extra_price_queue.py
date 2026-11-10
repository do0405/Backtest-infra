"""Exercise Extra cadence admission and retirement in the isolated runtime."""
from datetime import date, datetime, timezone
import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture
def cadence(monkeypatch):
    source = os.environ.get('EXTRA_RUNTIME_SOURCE')
    if not source:
        pytest.skip('EXTRA_RUNTIME_SOURCE must point to the extracted extra runtime')
    monkeypatch.syspath_prepend(str(Path(source) / 'src'))
    return importlib.import_module('backtest_data.remote.extra_cadence')


AS_OF = datetime(2026, 11, 11, 2, tzinfo=timezone.utc)
START = date(2026, 11, 9)
MEMBERS = [dict(security_id=entity, market='KR') for entity in ('a', 'b', 'c')]


def task(entity, day, lane, *, status='pending', checkpoint=None):
    return dict(source='kis', domain='prices', entity_id=entity,
                identity_hash=entity+day+lane, id=entity+day+lane, status=status,
                request=dict(market='KR', symbol=entity, manifest_id='fixture',
                             output_start=day, output_end=day, cadence_lane=lane,
                             refresh_policy=f'extra_{lane}_v6'),
                checkpoint=checkpoint or {})


def test_daily_catchup_admits_only_missing_requests_and_respects_capacity(cadence, monkeypatch):
    # The existing latest request rolls into the history queue automatically.
    existing = task('a', '2026-11-09', 'daily_latest')
    checkpoint = {'task_state': [existing]}
    templates = [dict(source='kis', domain='prices', entity_id=row['security_id'],
                      request=dict(market='KR', symbol=row['security_id'], manifest_id='fixture'))
                 for row in MEMBERS]
    monkeypatch.setattr(cadence, 'build_update_jobs', lambda *args, **kwargs: templates)
    monkeypatch.setattr(cadence, 'MAX_DAILY_JOBS_PER_LANE', 2)
    jobs, _ = cadence.plan_price_cadence(MEMBERS, [], manifest_id='fixture', original_scope={},
        as_of=AS_OF, sources={'kis'}, checkpoint=checkpoint, forward_start=START, mappings={})
    catchup = [job['entity_id'] for job in jobs if job['request']['cadence_lane'] == 'daily_catchup']
    latest = [job['entity_id'] for job in jobs if job['request']['cadence_lane'] == 'daily_latest']
    assert catchup == ['b']
    assert latest == ['a', 'b']
    admitted = task('b', '2026-11-09', 'daily_catchup', status='succeeded')
    checkpoint['task_state'].append(admitted)
    jobs, _ = cadence.plan_price_cadence(MEMBERS, [], manifest_id='fixture', original_scope={},
        as_of=AS_OF, sources={'kis'}, checkpoint=checkpoint, forward_start=START, mappings={})
    assert [job['entity_id'] for job in jobs if job['request']['cadence_lane'] == 'daily_catchup'] == ['c']


def test_retire_completed_daily_rows_without_losing_progress_or_live_requests(cadence):
    settled = [task(entity, '2026-11-09', 'daily_latest', status='succeeded',
                    checkpoint={'source_rows_total': 1} if entity == 'a' else {'response_valid': True})
               for entity in ('a', 'b', 'c')]
    open_task = task('a', '2026-11-10', 'daily_latest')
    future_success = task('b', '2026-11-10', 'daily_latest', status='succeeded')
    checkpoint = dict(task_state=[*settled, open_task, future_success],
                      completed_task_ids=[row['identity_hash'] for row in [*settled, future_success]],
                      completed_through={})
    proof = cadence.price_progress(MEMBERS, checkpoint, sources={'kis'}, forward_start=START, as_of=AS_OF)
    assert proof['kis']['request_completed_through'] == '2026-11-09'
    assert cadence.retire_completed_daily_jobs(checkpoint, proof) == 3
    assert checkpoint['task_state'] == [open_task, future_success]
    assert checkpoint['completed_task_ids'] == [future_success['identity_hash']]
    assert cadence.price_progress(MEMBERS, checkpoint, sources={'kis'}, forward_start=START, as_of=AS_OF) == proof


def test_rolled_daily_backlog_cannot_grow_without_admission_capacity(cadence, monkeypatch):
    backlog = [task(entity, day, 'daily_latest') for day in ('2026-11-07', '2026-11-08')
               for entity in ('a', 'b')]
    monkeypatch.setattr(cadence, 'MAX_DAILY_JOBS_PER_LANE', 2)
    monkeypatch.setattr(cadence, 'build_update_jobs', lambda *args, **kwargs: [
        dict(source='kis', domain='prices', entity_id='a',
             request=dict(market='KR', symbol='a', manifest_id='fixture'))])
    jobs, progress = cadence.plan_price_cadence(MEMBERS, [], manifest_id='fixture', original_scope={},
        as_of=AS_OF, sources={'kis'}, checkpoint={'task_state': backlog}, forward_start=START, mappings={})
    assert jobs == []
    assert progress['source_progress']['kis']['first_unadmitted_date'] == '2026-11-09'


def test_daily_alias_keeps_changed_provider_request_distinct(cadence):
    prior = task('a', '2026-11-09', 'daily_latest')
    same = task('a', '2026-11-09', 'daily_catchup')
    assert cadence._daily_identity(prior) == cadence._daily_identity(same)
    same['request']['symbol'] = 'new-mapping'
    assert cadence._daily_identity(prior) != cadence._daily_identity(same)


def test_history_selection_can_use_more_than_the_previous_128_job_limit(cadence):
    jobs = [task(str(i), '2026-11-09', 'daily_catchup') for i in range(160)]
    queue, selected = cadence.history_rotation(jobs, [], source='kis', as_of=AS_OF)
    assert selected == queue
    assert len(selected) == 160


def test_unused_latest_time_is_available_to_history(cadence, monkeypatch):
    now = [0.]
    calls = []
    monkeypatch.setattr(cadence.time, 'monotonic', lambda: now[0])

    def collect(*args, request_lane, budget_seconds, **kwargs):
        calls.append((request_lane, budget_seconds))
        now[0] += 5 if request_lane == 'latest' else budget_seconds
        return {}

    cadence.run_price_budget(collect, None, None, dict(source='kis', market='KR', domain='prices'),
                             budget_seconds=100, as_of=AS_OF, hard_deadline=100, history_ids=['job'])
    assert calls == [('latest', 60), ('history', 80), ('correction', 15)]
