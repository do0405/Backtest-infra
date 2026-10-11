from datetime import datetime, timedelta, timezone
import copy
import json

import pytest

NOW = datetime(2026, 10, 12, 1, tzinfo=timezone.utc)
BINDING = {'stream_id': 'krx-test', 'manifest_id': 'manifest-test',
           'handoff_id': 'handoff-test', 'bootstrap_sha256': 'a' * 64}


def receipt(run_id='1', credits=608, *, now=NOW):
    return {'format': 'krx_quota_allocation_v1', **BINDING, 'run_id': run_id,
            'run_attempt': 1, 'created_at': now.isoformat(),
            'expires_at': (now + timedelta(hours=2)).isoformat(),
            'daily_limit': 9500, 'credits_by_day': {'2026-10-12': credits}}


def test_allocation_does_not_refund_failed_previous_run():
    from backtest_data.remote.quota import plan_allocation
    prior = receipt(credits=9400)
    value = plan_allocation(BINDING, [prior], [], run_id='2', run_attempt=1,
                            now=NOW, expires_at=NOW + timedelta(hours=2), budget_seconds=6000)
    assert value['credits_by_day'] == {'2026-10-12': 100}


def test_repeated_hosted_attempt_cannot_reuse_reserved_credits():
    from backtest_data.remote.quota import plan_allocation
    assert plan_allocation(BINDING, [receipt()], [], run_id='1', run_attempt=1,
                           now=NOW, expires_at=NOW + timedelta(hours=2), budget_seconds=300) is None


def test_possible_midnight_days_are_both_reserved():
    from backtest_data.remote.quota import plan_allocation
    now = datetime(2026, 10, 12, 14, 30, tzinfo=timezone.utc)
    value = plan_allocation(BINDING, [], [], run_id='2', run_attempt=1, now=now,
                            expires_at=now + timedelta(hours=2), budget_seconds=300)
    assert value['credits_by_day'] == {'2026-10-12': 38, '2026-10-13': 38}


def test_legacy_workflow_usage_is_charged_conservatively():
    from backtest_data.remote.quota import plan_allocation
    legacy = {'id': 9, 'run_attempt': 1, 'status': 'completed',
              'run_started_at': (NOW - timedelta(hours=1)).isoformat(),
              'updated_at': (NOW - timedelta(minutes=10)).isoformat()}
    prior = receipt(credits=9150)
    value = plan_allocation(BINDING, [prior], [legacy], run_id='2', run_attempt=1,
                            now=NOW, expires_at=NOW + timedelta(hours=2), budget_seconds=6000)
    # 50 minutes at a ten-second minimum plus eight conservative slots.
    assert value['credits_by_day'] == {'2026-10-12': 42}


@pytest.mark.parametrize('change', [
    {'handoff_id': 'wrong'}, {'daily_limit': 10000},
    {'credits_by_day': {'2026-10-14': 30}}, {'credits_by_day': {'2026-10-12': True}},
    {'api_key': 'never-accepted'},
])
def test_invalid_or_rebound_receipt_is_rejected(change):
    from backtest_data.remote.quota import validate_allocation
    value = receipt(); value.update(change)
    with pytest.raises(ValueError):
        validate_allocation(value, BINDING)


def test_exhausted_daily_budget_admits_no_http():
    from backtest_data.remote.quota import plan_allocation
    assert plan_allocation(BINDING, [receipt(credits=9500)], [], run_id='2', run_attempt=1,
                           now=NOW, expires_at=NOW + timedelta(hours=2), budget_seconds=300) is None


def test_quota_releases_are_outside_existing_data_inventory():
    from backtest_data.remote import releases
    from unittest.mock import patch
    values = [[{'tag_name': 'quota-krx-test-r1-a1', 'draft': False, 'prerelease': False},
               {'tag_name': 'data-krx-test-000000011-r1-a1', 'draft': False, 'prerelease': False}]]
    with patch.object(releases, 'github', return_value=json.dumps(values).encode()):
        assert len(releases.inventory('gh', 'a/b', 'krx-test')) == 1


def test_published_allocation_requires_exact_readback_before_return(tmp_path, monkeypatch):
    from backtest_data.remote import quota
    events = []
    expected = receipt()
    release = {'draft': False, 'tag_name': 'quota-krx-test-r1-a1', 'assets': []}
    def github(gh, arguments, **kwargs):
        events.append(arguments[:2])
        if arguments[0] == 'api':
            return json.dumps(release).encode()
        return b''
    def download(*args, **kwargs):
        destination = tmp_path / 'readback.json'
        destination.write_text(json.dumps(expected), encoding='utf-8')
        events.append(['verified', 'download'])
        return destination
    monkeypatch.setattr(quota, 'github', github)
    monkeypatch.setattr(quota, 'download_asset', download)
    assert quota.publish_allocation('gh', 'a/b', expected, tmp_path, target='b' * 40) == expected
    assert events == [['release', 'create'], ['release', 'edit'], ['api', 'repos/a/b/releases/tags/quota-krx-test-r1-a1'], ['verified', 'download']]


def test_mismatched_or_unpublished_allocation_never_authorizes(tmp_path, monkeypatch):
    from backtest_data.remote import quota
    expected = receipt()
    monkeypatch.setattr(quota, 'github', lambda *args, **kwargs: json.dumps({'draft': True}).encode())
    monkeypatch.setattr(quota, 'download_asset', lambda *args, **kwargs: pytest.fail('draft downloaded'))
    with pytest.raises(ValueError, match='unpublished'):
        quota.publish_allocation('gh', 'a/b', expected, tmp_path, target='b' * 40)
