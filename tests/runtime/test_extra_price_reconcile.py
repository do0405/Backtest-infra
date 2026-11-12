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
    assert result['KR']['retry'] == dict(waiting=1, eligible=1, market_closed=0,
                                         stored_daily_bar=0, conflicting_evidence=0)
    assert checkpoint['task_state'][0]['status'] == 'retry'
