from copy import deepcopy
from datetime import date, datetime, timezone

from importlib import import_module
import os
from pathlib import Path

import pytest


@pytest.fixture
def reconciliation(monkeypatch):
    source = os.environ.get('EXTRA_RUNTIME_SOURCE')
    if not source:
        pytest.skip('EXTRA_RUNTIME_SOURCE must point to the extracted extra runtime')
    monkeypatch.syspath_prepend(str(Path(source) / 'src'))
    module = import_module('backtest_data.remote.extra_price_reconcile')
    assert Path(module.__file__).is_relative_to(Path(source).resolve())
    return module


def task(identity, market, status, day, **changes):
    request = dict(market=market, output_start=day, output_end=day,
                   cadence_lane='daily_catchup', refresh_policy='extra_daily_catchup_v6')
    if market == 'US':
        request.update(exchange='NAS', provider_symbol='EXACT', mapping_status='qualified')
    request.update(changes)
    return dict(id=identity, identity_hash=identity, source='kis' if market == 'KR' else 'kis_overseas',
                entity_id=identity.split('-')[0], domain='prices', request=request,
                status=status, checkpoint={'reason': '403'} if status == 'retry' else {})


def test_only_verified_past_single_session_requests_are_cleaned(reconciliation):
    tasks = [
        task('kr-closed', 'KR', 'retry', '2026-10-09'),
        task('us-closed', 'US', 'pending', '2026-11-26'),
        task('us-saved', 'US', 'retry', '2026-11-27'),
        task('us-different-route', 'US', 'retry', '2026-11-27', exchange='NYS'),
        task('us-current', 'US', 'pending', '2026-12-01'),
        task('kr-multi', 'KR', 'retry', '2026-10-09', output_start='2026-10-08'),
        task('kr-correction', 'KR', 'pending', '2026-10-09', cadence_lane='weekly_correction',
             refresh_policy='extra_weekly_correction_v6'),
        task('kr-open', 'KR', 'pending', '2026-11-12'),
        task('other-contradiction', 'KR', 'pending', '2026-10-09'),
    ]
    checkpoint = {'task_state': tasks, 'unfinished_task_ids': [x['identity_hash'] for x in tasks],
                  'completed_task_ids': ['existing'], 'price_history_queue': {
                      'kis': ['kr-closed', 'kr-open'], 'kis_overseas': ['us-saved', 'us-different-route']}}
    before = deepcopy(checkpoint)
    dates = dict.fromkeys(('kis', 'kis_overseas'), date(2026, 12, 1))
    saved = {('US', 'us', date(2026, 11, 27), 'NAS', 'EXACT'),
             ('US', 'us', date(2026, 12, 1), 'NAS', 'EXACT'),
             ('KR', 'other', date(2026, 10, 9), 'KRX', None)}
    stats = reconciliation.reconcile_price_queue(checkpoint, ready_dates=dates, stored=saved)

    assert stats['KR']['retry']['market_closed'] == 1
    assert stats['KR']['pending']['conflicting_evidence'] == 1
    assert stats['US']['pending']['market_closed'] == 1
    assert stats['US']['retry']['stored_daily_bar'] == 1
    assert [x['status'] for x in tasks[:3]] == ['superseded', 'superseded', 'succeeded']
    assert tasks[3:] == before['task_state'][3:]
    assert checkpoint['completed_task_ids'] == ['existing', 'us-saved']
    assert checkpoint['unfinished_task_ids'] == before['unfinished_task_ids'][3:]
    assert checkpoint['price_history_queue'] == {'kis': ['kr-open'], 'kis_overseas': ['us-different-route']}
    assert all(not counts['stored_daily_bar'] and not counts['market_closed']
               for by_status in reconciliation.reconcile_price_queue(checkpoint, ready_dates=dates, stored=saved).values()
               for counts in by_status.values())


def test_only_contract_verified_observations_match_exact_us_route(reconciliation):
    day = date(2026, 11, 10)
    observation = dict(id='bar', security_id='us', session=day, source_contract_id='contract',
                       basis='as_traded', venue='NAS', feed='kis:us:NAS', currency='USD',
                       evidence={'source_symbol': 'EXACT'})
    contract = dict(id='contract', source='kis_overseas', market='US', domain='prices')
    decision = dict(record_id='bar', record_domain='price_observation', status='accepted',
                    evidence_grade='contract_verified', rule_version='kis_overseas_raw_price_structure_v1')

    def rows(price, review):
        return [dict(table='price_observation', values=price),
                dict(table='selection_decision', values=review),
                dict(table='source_contract', values=contract)]

    key = ('US', 'us', day, 'NAS', 'EXACT')
    assert reconciliation.stored_price_keys(rows(observation, decision), {key}) == {key}
    assert reconciliation.stored_price_keys(rows(observation, {**decision, 'status': 'rejected'}), {key}) == set()
    assert reconciliation.stored_price_keys(rows({**observation, 'basis': 'adjusted'}, decision), {key}) == set()
    assert reconciliation.stored_price_keys(rows({**observation, 'venue': 'NYS'}, decision), {key}) == set()
    assert reconciliation.stored_price_keys(rows({**observation, 'evidence': {'source_symbol': 'OTHER'}}, decision), {key}) == set()



def test_confirmed_holidays_are_not_readmitted_and_early_close_is_a_session(reconciliation):
    cadence = import_module('backtest_data.remote.extra_cadence')
    as_of = datetime(2026, 11, 28, 15, tzinfo=timezone.utc)
    assert cadence.price_ready_date('kis_overseas', as_of) == date(2026, 11, 27)
    progress = cadence.price_progress(
        [{'market': 'KR', 'security_id': 'kr'}], {}, sources={'kis'},
        forward_start=date(2026, 10, 9),
        as_of=datetime(2026, 10, 13, 3, tzinfo=timezone.utc))
    assert progress['kis']['request_completed_through'] == '2026-10-11'
    assert progress['kis']['first_unadmitted_date'] == '2026-10-12'
    assert not reconciliation.market_closed('US', date(2026, 11, 27))


def test_missing_predecessor_price_evidence_leaves_open_day_pending(reconciliation):
    checkpoint = {'task_state': [task('kr-open', 'KR', 'retry', '2026-11-12')]}
    result = reconciliation.reconcile_price_queue(
        checkpoint, ready_dates={'kis': date(2026, 11, 13)}, stored=set())
    counts = result['KR']['retry']
    assert counts['unfinished'] == counts['eligible'] == 1
    assert not any(value for field, value in counts.items() if field not in {'unfinished', 'eligible'})
    assert checkpoint['task_state'][0]['status'] == 'retry'


def test_counts_and_cleans_only_proven_invalid_requests(reconciliation):
    weekend = task('weekend-old', 'KR', 'retry', '2026-11-08', cadence_lane=None,
                   refresh_policy='catchup_and_rolling14_calendar_days_v1')
    holiday = task('holiday-old', 'KR', 'unsupported', '2026-10-09')
    conflicting = task('conflict-old', 'US', 'retry', '2026-11-26')
    conflicting['checkpoint']['normalized_rows_total'] = 1
    current = task('current-old', 'KR', 'pending', '2026-12-01')
    multi = task('multi-old', 'KR', 'retry', '2026-11-08', output_start='2026-11-06')
    left = task('same-left', 'US', 'retry', '2026-11-27')
    right = task('same-right', 'US', 'pending', '2026-11-27', cadence_lane='daily_latest',
                 refresh_policy='extra_daily_latest_v6')
    right['attempts'] = 0
    progressed = task('keep-left', 'US', 'retry', '2026-11-27')
    other_progress = task('keep-right', 'US', 'retry', '2026-11-27', cadence_lane='daily_latest',
                          refresh_policy='extra_daily_latest_v6')
    alternate_route = task('same-alt', 'US', 'retry', '2026-11-27', exchange='NYS')
    tasks = [weekend, holiday, conflicting, current, multi, left, right, progressed, other_progress, alternate_route]
    checkpoint = {'task_state': tasks, 'unfinished_task_ids': [x['identity_hash'] for x in tasks],
                  'price_history_queue': {'kis': [weekend['id'], multi['id']],
                                          'kis_overseas': [right['id'], left['id'], progressed['id']]}}
    dates = dict.fromkeys(('kis', 'kis_overseas'), date(2026, 12, 1))

    stats = reconciliation.reconcile_price_queue(checkpoint, ready_dates=dates, stored=set())

    assert sum(counts['unfinished'] for market in stats.values() for counts in market.values()) == 10
    assert stats['KR']['retry']['weekend'] == stats['KR']['retry']['removed_weekend'] == 1
    assert stats['KR']['unsupported']['holiday'] == stats['KR']['unsupported']['removed_holiday'] == 1
    assert stats['US']['retry']['holiday'] == stats['US']['retry']['conflicting_evidence'] == 1
    assert stats['US']['retry']['removed_holiday'] == 0
    assert stats['US']['pending']['duplicate'] == stats['US']['pending']['removed_duplicate'] == 1
    assert stats['US']['retry']['duplicate'] == 1 and stats['US']['retry']['removed_duplicate'] == 0
    assert {x['id'] for x in tasks if x['status'] == 'superseded'} == {'weekend-old', 'holiday-old', 'same-right'}
    assert weekend['checkpoint']['reason'] == 'verified_exchange_closed'
    assert checkpoint['price_history_queue'] == {'kis': [multi['id']], 'kis_overseas': [left['id'], progressed['id']]}
    assert set(checkpoint['unfinished_task_ids']) == {x['identity_hash'] for x in tasks if x['status'] != 'superseded'}


def test_duplicate_cleanup_requires_a_viable_exact_survivor(reconciliation):
    same = {'cadence_lane': 'daily_latest', 'refresh_policy': 'extra_daily_latest_v6'}
    attempted_failure = task('need-left', 'US', 'failed', '2026-11-27')
    live = task('need-right', 'US', 'pending', '2026-11-27', **same)
    live['attempts'] = 0
    saved = task('saved-left', 'US', 'succeeded', '2026-11-27')
    saved['checkpoint'] = {'normalized_rows_total': 1}
    redundant = task('saved-right', 'US', 'pending', '2026-11-27', **same)
    redundant['attempts'] = 0
    checkpoint = {'task_state': [attempted_failure, live, saved, redundant]}

    result = reconciliation.reconcile_price_queue(
        checkpoint, ready_dates={'kis_overseas': date(2026, 11, 30)}, stored=set())

    assert result['US']['failed']['duplicate'] == 1
    assert attempted_failure['status'] == 'failed' and live['status'] == 'pending'
    assert result['US']['pending']['removed_duplicate'] == 1 and redundant['status'] == 'superseded'


def test_range_audit_preserves_any_trading_day_and_only_exact_aliases(reconciliation):
    weekly = {'cadence_lane': 'weekly_correction', 'refresh_policy': 'extra_weekly_correction_v6'}
    legacy = {'cadence_lane': None, 'refresh_policy': 'catchup_and_rolling14_calendar_days_v1'}
    weekend = task('weekend-range', 'KR', 'pending', '2026-10-18', output_start='2026-10-17', **legacy)
    holiday = task('holiday-range', 'KR', 'retry', '2026-10-11', output_start='2026-10-09', **legacy)
    trading = task('trading-range', 'KR', 'pending', '2026-10-11', output_start='2026-10-08', **legacy)
    conflict = task('conflict-range', 'KR', 'retry', '2026-10-11', output_start='2026-10-09', **legacy)
    range_options = dict(output_start='2026-11-14', correction_anchor='2026-11-27', **weekly)
    first = task('same-first', 'US', 'retry', '2026-11-27', **range_options)
    alias = task('same-alias', 'US', 'pending', '2026-11-27', **range_options)
    alias['attempts'] = 0
    other = task('same-other', 'US', 'pending', '2026-11-27', **{**range_options, 'output_start': '2026-11-15'})
    other['attempts'] = 0
    unknown_route = task('us-unknown', 'US', 'unsupported', '2026-11-27', mapping_status='unsupported')
    too_recent = task('recent-day', 'US', 'pending', '2026-11-30')
    tasks = [weekend, holiday, trading, conflict, first, alias, other, unknown_route, too_recent]
    cutoff = dict.fromkeys(('kis', 'kis_overseas'), date(2026, 11, 30))

    scope = reconciliation.prior_scope_counts(tasks, ready_dates=cutoff)
    assert scope['KR']['multi_date_legacy_rolling'] == 4
    assert scope['US']['multi_date_weekly_correction'] == 3
    assert scope['US']['unqualified_route'] == scope['US']['not_past_ready_date'] == 1
    stored = {('KR', 'conflict', date(2026, 10, 9), 'KRX', None)}
    assert stored <= reconciliation.candidate_price_keys(tasks, cutoff)

    result = reconciliation.reconcile_price_queue({'task_state': tasks}, ready_dates=cutoff, stored=stored)
    assert result['KR']['pending']['removed_weekend'] == 1
    assert result['KR']['retry']['removed_holiday'] == 1
    assert result['KR']['retry']['conflicting_evidence'] == 1
    assert result['US']['pending']['removed_duplicate'] == 1
    assert {row['id'] for row in tasks if row['status'] == 'superseded'} == {
        'weekend-range', 'holiday-range', 'same-alias',
    }


def test_mapping_repair_requires_dated_identity_and_rechecks_new_request(reconciliation):
    from backtest_data.remote.extra_price_mapping import audit_and_repair_us_mappings

    def evidence(symbol, stamp='2026-11-05T15:00:00+00:00', exchange='NAS'):
        return {'id': 'evidence-' + symbol + exchange, 'fields': {
            'provider_symbol': symbol, 'exchange': exchange, 'market': 'US',
            'master_realtime_symbol': exchange + symbol, 'realtime_symbol': 'D' + exchange + symbol,
            'qualified_current_mapping': True, 'master_hash': 'a' * 64, 'master_raw_id': 'raw',
            'master_captured_at': stamp,
        }}

    rows = []
    members, observations = {}, {}
    for symbol in ('GOOD', 'LATE', 'COLLIDE', 'MISSING', 'PROGRESS'):
        row = task(symbol+'-old', 'US', 'pending', '2026-11-08', symbol=symbol,
                   mapping_status='unsupported', provider_symbol=None, exchange=None,
                   mapping_reason='restored_exact_master_mapping_unavailable')
        row['attempts'] = 0
        rows.append(row)
        members[symbol] = {'symbol': symbol, 'identifier': 'directory:' + symbol, 'security_id': symbol, 'market': 'US'}
        observations['directory:' + symbol] = [evidence(symbol)]
    observations['directory:LATE'] = [evidence('LATE', '2026-11-09T15:00:00+00:00')]
    observations['directory:COLLIDE'].append(evidence('COLLIDE', exchange='NYS'))
    observations.pop('directory:MISSING')
    rows[-1]['checkpoint']['source_rows_total'] = 2
    archive = {'members': members, 'observations': observations}
    ready = {'kis_overseas': date(2026, 11, 10)}
    checkpoint = {'task_state': rows, 'unfinished_task_ids': [x['identity_hash'] for x in rows],
                  'price_history_queue': {'kis_overseas': [x['id'] for x in rows]}}

    summary = audit_and_repair_us_mappings(checkpoint, archive, ready_dates=ready, apply=True)

    assert summary['applied'] == 1
    assert summary['reasons'] == {'verified_catalog_route': 1, 'catalog_capture_after_window_start': 1,
                                  'conflicting_catalog_routes': 1, 'no_archived_master_record': 1,
                                  'prior_collection_evidence': 1}
    replacement = checkpoint['task_state'][-1]
    assert rows[0]['status'] == 'superseded' and rows[0]['request']['mapping_status'] == 'unsupported'
    assert replacement['entity_id'] == 'GOOD' and replacement['request']['exchange'] == 'NAS'
    assert replacement['identity_hash'] != rows[0]['identity_hash']
    assert checkpoint['price_history_queue']['kis_overseas'][0] == replacement['id']
    assert replacement['identity_hash'] in checkpoint['unfinished_task_ids']
    assert audit_and_repair_us_mappings(checkpoint, archive, ready_dates=ready, apply=True)['applied'] == 0
    result = reconciliation.reconcile_price_queue(checkpoint, ready_dates=ready, stored=set())
    assert result['US']['pending']['removed_weekend'] == 1
    assert replacement['status'] == 'superseded'
