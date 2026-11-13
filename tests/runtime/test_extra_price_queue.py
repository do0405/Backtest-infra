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


def test_full_history_queue_does_not_block_latest(cadence, monkeypatch):
    backlog = [task(entity, day, 'daily_latest') for day in ('2026-11-07', '2026-11-08')
               for entity in ('a', 'b')]
    monkeypatch.setattr(cadence, 'MAX_DAILY_JOBS_PER_LANE', 2)
    monkeypatch.setattr(cadence, 'build_update_jobs', lambda *args, **kwargs: [
        dict(source='kis', domain='prices', entity_id='a',
             request=dict(market='KR', symbol='a', manifest_id='fixture'))])
    jobs, progress = cadence.plan_price_cadence(MEMBERS, [], manifest_id='fixture', original_scope={},
        as_of=AS_OF, sources={'kis'}, checkpoint={'task_state': backlog}, forward_start=START, mappings={})
    assert [job['request']['cadence_lane'] for job in jobs] == ['daily_latest']
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


def test_both_markets_run_latest_before_any_history(cadence, monkeypatch):
    now = [0.]
    calls = []
    monkeypatch.setattr(cadence.time, 'monotonic', lambda: now[0])

    def collect(*args, source_filter, request_lane, **kwargs):
        calls.append((source_filter[0], request_lane))
        now[0] += 1
        return {}

    slots = [dict(source='kis', market='KR', domain='prices'),
             dict(source='kis_overseas', market='US', domain='prices')]
    cadence.run_price_slots(collect, None, None, slots, budget_seconds=100,
                            as_of=AS_OF, hard_deadline=100, history_ids={})
    assert calls[:4] == [('kis', 'latest'), ('kis_overseas', 'latest'),
                         ('kis', 'history'), ('kis_overseas', 'history')]


def test_legacy_cleanup_preserves_attempted_and_nonidentical_jobs(cadence):
    finished = task('a', '2026-11-09', 'daily_latest', status='succeeded')
    duplicate = task('a', '2026-11-09', 'daily_catchup')
    attempted = task('b', '2026-11-09', 'daily_catchup', status='retry')
    weekend = task('c', '2026-11-08', 'daily_catchup')
    weekend['attempts'] = duplicate['attempts'] = 0
    attempted['attempts'] = 2
    checkpoint = dict(task_state=[finished, duplicate, attempted, weekend],
                      price_history_queue={'kis': [duplicate['id'], attempted['id'], weekend['id']]})
    assert cadence.clean_legacy_daily_queue(checkpoint, as_of=AS_OF) == {
        'duplicate_unstarted': 1, 'weekend_unstarted': 1}
    assert checkpoint['task_state'] == [finished, attempted]
    assert checkpoint['price_history_queue']['kis'] == [attempted['id']]


def test_new_untouched_latest_jobs_remain_plannable_instead_of_persisting(cadence):
    untouched = task('a', '2026-11-10', 'daily_latest')
    progressed = task('b', '2026-11-10', 'daily_latest', status='retry')
    old = task('c', '2026-11-09', 'daily_latest')
    untouched['attempts'] = old['attempts'] = 0
    progressed['attempts'] = 1
    rows, count = cadence.defer_unstarted_latest([untouched, progressed, old],
                                                 {untouched['id'], progressed['id']})
    assert (rows, count) == ([progressed, old], 1)


def test_history_reduction_requires_actual_source_rows(cadence):
    old = [task(name, '2026-11-09', 'daily_latest') for name in ('a', 'b', 'c')]
    old[0]['checkpoint']['normalized_rows_total'] = 2
    after = [dict(old[0], status='succeeded', checkpoint={'normalized_rows_total': 6}),
             dict(old[1], status='unsupported'), old[2]]
    assert cadence.history_progress(old, after, as_of=AS_OF) == {
        'starting': 3, 'succeeded_with_rows': 1, 'succeeded_without_rows': 0,
        'unsupported': 1, 'source_rows': 4, 'still_unfinished': 2}


def test_today_catchup_alias_is_served_as_latest(cadence):
    from sqlalchemy.dialects import postgresql
    from backtest_data.ingestion.jobs import acquire_job

    class Session:
        def scalar(self, statement):
            self.sql = str(statement.compile(dialect=postgresql.dialect(),
                                             compile_kwargs={'literal_binds': True}))

    old = task('a', '2026-11-09', 'daily_catchup')
    current = task('b', '2026-11-10', 'daily_catchup')
    assert cadence.history_rotation([current, old], [], source='kis', as_of=AS_OF)[1] == [old['id']]
    session = Session()
    from datetime import timedelta
    acquire_job(session, 'worker', AS_OF, timedelta(minutes=5), sources=['kis'], domains=['prices'],
                request_lane='latest', request_day=date(2026, 11, 10))
    assert "request->>'cadence_lane' IN ('daily_latest','daily_catchup')" in session.sql
    assert "'2026-11-10'" in session.sql


@pytest.mark.parametrize(('clock', 'ready'), [
    ('2026-11-11T01:29:59+00:00', '2026-11-09'),
    ('2026-11-11T01:30:00+00:00', '2026-11-10'),
    ('2026-11-12T01:30:00+00:00', '2026-11-11'),
    ('2026-11-16T01:30:00+00:00', '2026-11-13'),
    ('2026-07-15T01:30:00+00:00', '2026-07-14'),
])
def test_extra_us_ready_cutoff_matches_1030_korea(cadence, clock, ready):
    assert cadence.price_ready_date('kis_overseas', datetime.fromisoformat(clock)).isoformat() == ready


def test_early_manual_us_run_retains_older_history(cadence):
    previous = dict(task('old', '2026-11-06', 'daily_catchup'), source='kis_overseas')
    latest_ready = dict(task('ready', '2026-11-09', 'daily_latest'), source='kis_overseas')
    held = dict(task('hold', '2026-11-10', 'daily_latest'), source='kis_overseas')
    clock = datetime.fromisoformat('2026-11-11T01:29:00+00:00')
    assert cadence.history_rotation([held, latest_ready, previous], [], source='kis_overseas',
                                    as_of=clock)[1] == [previous['id']]


def test_1030_cutoff_is_enforced_by_us_request_adapter(cadence):
    from backtest_data.prices.adapters.kis_overseas import KISOverseasDailyRequest

    day = date(2026, 11, 10)
    with pytest.raises(ValueError):
        KISOverseasDailyRequest('SPY', day, day, 'AMS',
                               captured_at=datetime.fromisoformat('2026-11-11T01:29:59+00:00'),
                               use_seoul_readiness=True)
    request = KISOverseasDailyRequest('SPY', day, day, 'AMS',
                                      captured_at=datetime.fromisoformat('2026-11-11T01:30:00+00:00'),
                                      use_seoul_readiness=True)
    assert request.params()['BYMD'] == '20261110'


def test_coalescing_keeps_exact_security_mapping_and_only_completes_observed_days(cadence):
    from types import SimpleNamespace
    from backtest_data.ingestion.jobs import coalesce_price_window, observed_price_requests

    leader = SimpleNamespace(**task('a', '2026-11-10', 'daily_latest'))
    past = SimpleNamespace(**task('a', '2026-11-09', 'daily_catchup'), attempts=0)
    absent = SimpleNamespace(**task('a', '2026-11-06', 'daily_catchup'), attempts=0)
    future = SimpleNamespace(**task('a', '2026-11-11', 'daily_latest'), attempts=0)
    wrong_mapping = SimpleNamespace(**task('a', '2026-11-05', 'daily_latest'), attempts=0)
    wrong_mapping.request['symbol'] = 'new-provider-symbol'
    attempted = SimpleNamespace(**task('a', '2026-11-04', 'daily_catchup'), attempts=2)
    window = coalesce_price_window(leader, [past, absent, future, wrong_mapping, attempted],
                                   ready_day=date(2026, 11, 10), lane='latest')
    assert window[:2] == (date(2026, 11, 6), date(2026, 11, 10))
    assert window[2] == {past.id: '2026-11-09', absent.id: '2026-11-06'}
    assert observed_price_requests(window[2], {'2026-11-09', '2026-11-10'}) == [past.id]


def test_coalesced_page_does_not_mark_a_missing_latest_bar_as_done(cadence, monkeypatch):
    from backtest_data.ingestion import supervisor

    finished = []
    def finish(session, siblings, observed):
        finished.extend(day for day in siblings.values() if day in observed)
        return len(finished)

    monkeypatch.setattr(supervisor, 'finish_observed_price_requests', finish)
    prior = '2026-11-09'
    latest = '2026-11-10'
    window = (date.fromisoformat(prior), date.fromisoformat(latest), {'sibling': prior})
    outcome = {'observed_sessions': [prior], 'rejected': 0,
               'response_valid': True, 'pagination_valid': True, 'source_row_count': 99}
    request = {'output_start': latest, 'output_end': latest, 'cadence_lane': 'daily_latest'}
    assert supervisor._record_kis_page(None, request, {}, outcome, window) == ('retry', 0, 1)
    assert finished == [prior]
    assert outcome['normalized_rows_total'] == 0
    assert outcome['partial_daily_retry'] is True
    assert supervisor._kis_price_bounds(request, outcome, None) == (date(2026, 11, 10), date(2026, 11, 10))
    retry = {'observed_sessions': [], 'rejected': 0, 'response_valid': True, 'pagination_valid': True}
    assert supervisor._record_kis_page(None, request, outcome, retry, None) == ('unsupported', 0, 0)
    assert finished == [prior]


def test_legacy_kis_paginates_then_retries_only_unobserved_sessions(cadence):
    from types import SimpleNamespace
    from backtest_data.ingestion import supervisor
    from backtest_data.remote.checkpoint import runtime_task, snapshot_jobs

    request = dict(market='US', refresh_policy='catchup_and_rolling14_calendar_days_v1',
                   output_start='2026-11-23', output_end='2026-11-27')

    def record(before, observed, complete=True):
        outcome = dict(observed_sessions=observed, response_valid=True, pagination_valid=True,
                       traversal_complete=complete, source_rows_total=len(observed))
        result = supervisor._record_kis_page(None, request, before, outcome, None)
        return result[0], outcome

    status, first = record({}, ['2026-11-27'], False)
    assert status is None
    status, partial = record(first, ['2026-11-23', '2026-11-25'])
    assert status == 'retry'
    job = SimpleNamespace(id='job', identity_hash='hash', source='kis_overseas',
                          domain='prices', entity_id='entity', request=request,
                          priority=1, status=status, attempts=2, checkpoint=partial)
    [snapshot] = snapshot_jobs([job], owned_sources={'kis_overseas'})
    restored = runtime_task(snapshot)['checkpoint']
    assert supervisor._kis_price_bounds(request, restored, None) == (date(2026, 11, 24), date(2026, 11, 24))
    status, done = record(restored, ['2026-11-24'])
    assert status == 'succeeded'
    assert done['normalized_rows_total'] == 4
    assert 'price_gap_windows' not in done


def test_extra_coalescing_does_not_requery_a_day_without_pending_work(cadence):
    from types import SimpleNamespace
    from backtest_data.ingestion.jobs import coalesce_price_window

    leader = SimpleNamespace(**task('a', '2026-11-10', 'daily_latest'))
    prior = SimpleNamespace(**task('a', '2026-11-06', 'daily_catchup'), attempts=0)
    window = coalesce_price_window(leader, [prior], ready_day=date(2026, 11, 10), lane='latest')
    assert window == (date(2026, 11, 10), date(2026, 11, 10), {})


def test_extra_kis_lanes_share_one_token(cadence, monkeypatch):
    import json
    from types import SimpleNamespace
    from backtest_data.ingestion import supervisor

    requests = []
    closed = []

    class HTTP:
        def __init__(self, engine):
            pass

        def request(self, source, method, url, **kwargs):
            requests.append((source, method, url))
            return SimpleNamespace(content=json.dumps({'access_token': 'fixture-token', 'expires_in': 3600}).encode())

        def close(self):
            closed.append(True)

    key = SimpleNamespace(reveal=lambda: 'fixture')
    monkeypatch.setattr(supervisor, 'ProviderHTTP', HTTP)
    monkeypatch.setattr(supervisor, 'load_credentials',
                        lambda **kwargs: SimpleNamespace(kis_app_key=key, kis_app_secret=key))
    with supervisor.shared_kis_price_client(None) as kis:
        assert kis._headers()['authorization'] == 'Bearer fixture-token'
        assert kis._headers('HHDFS76240000')['authorization'] == 'Bearer fixture-token'
    assert [method for _, method, _ in requests] == ['POST']
    assert closed == [True]


def test_weekly_page_reuses_only_matching_observed_daily_requests(cadence, monkeypatch):
    from types import SimpleNamespace
    from backtest_data.ingestion import supervisor
    from backtest_data.ingestion.jobs import load_coalesced_price_window, observed_price_requests

    weekly = task('a', '2026-11-06', 'weekly_correction', checkpoint={'cursor_end': '2026-11-05'})
    weekly['request'].update(output_start='2026-11-02', correction_anchor='2026-11-06')
    leader = SimpleNamespace(**weekly)
    matched = SimpleNamespace(**task('a', '2026-11-03', 'daily_catchup'), attempts=0)
    missing = SimpleNamespace(**task('a', '2026-11-04', 'daily_catchup'), attempts=0)
    out_of_page = SimpleNamespace(**task('a', '2026-11-06', 'daily_catchup'), attempts=0)
    remapped = SimpleNamespace(**task('a', '2026-11-03', 'daily_latest'), attempts=0)
    remapped.request['symbol'] = 'different-provider-symbol'
    session = SimpleNamespace(scalars=lambda statement: [matched, missing, out_of_page, remapped])
    window = load_coalesced_price_window(session, leader, ready_day=date(2026, 11, 10),
                                         lane='correction')
    assert window == (date(2026, 11, 2), date(2026, 11, 5),
                      {matched.id: '2026-11-03', missing.id: '2026-11-04'})
    completed = []

    def finish(session, siblings, observed):
        completed.extend(observed_price_requests(siblings, observed))
        return len(completed)

    monkeypatch.setattr(supervisor, 'finish_observed_price_requests', finish)
    outcome = dict(observed_sessions=['2026-11-03'], response_valid=True, pagination_valid=True)
    result = supervisor._record_kis_page(session, leader.request, leader.checkpoint, outcome, window)
    assert result == (None, 1, 1)
    assert completed == [matched.id]
    assert outcome['normalized_rows_total'] == 1
    invalid = dict(observed_sessions=['2026-11-04'], response_valid=True, pagination_valid=False)
    assert supervisor._record_kis_page(session, leader.request, leader.checkpoint, invalid, window)[2] == 0
    assert completed == [matched.id]
