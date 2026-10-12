"""Publication must validate completely without repeating large payload passes."""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from contextlib import contextmanager
from types import SimpleNamespace as N
import json

import pytest

from backtest_data.remote import bundle, codec, folder

SCOPE = {'target_count': 17758, 'output_start': '2016-01-01', 'output_end': '2026-10-07'}


def fixture_bundle(root, *, sequence=1, previous=None, count=20):
    rows = ({'table': 'issuer', 'values': {'id': str(i), 'market': 'US',
            'source_identifier': str(i), 'name': 'Fixture'}} for i in range(count))
    return bundle.write_bundle(root, rows, stream_id='fixture', sequence=sequence,
        previous_bundle_id=previous, scope=SCOPE,
        checkpoint={'exact': Decimal('1234567890123456789.000000001'),
                    'captured_at': datetime(2026, 10, 12, tzinfo=timezone.utc)})


def test_decoding_checks_privacy_without_repacking_decoded_tree(monkeypatch):
    original = {'rows': [{'exact': Decimal('0.000000000001'),
                         'clock': datetime(2026, 10, 12, tzinfo=timezone.utc)}]}
    encoded = codec.dumps(original)
    monkeypatch.setattr(codec, 'pack', lambda _: pytest.fail('redundant decoded tree rebuild'))
    assert codec.loads(encoded) == original


@pytest.mark.parametrize('private', [
    {'url': 'https://example.com/path?accessToken=hidden'},
    {'url': 'https://user:password@example.com/path'},
    {'authorization': 'hidden'},
    {'next_page_token': 'untyped'},
    {'lease_owner': 'old'},
    {'bad': 0.1},
    {'bad': {'$type': 'datetime', 'value': '2026-10-12T00:00:00+09:00'}},
])
def test_late_private_or_inexact_decoded_values_still_fail(private):
    encoded = json.dumps({'rows': [{'safe': 'first'}] * 50 + [private]})
    with pytest.raises(ValueError):
        codec.loads(encoded)


def test_field_classification_cache_is_bounded_and_excludes_values():
    codec._private_field.cache_clear()
    for _ in range(100):
        assert not codec.private_field('OperatingLeaseCost')
        assert codec.private_field('accessToken')
    info = codec._private_field.cache_info()
    assert info.misses == 2 and info.hits == 198
    for i in range(5000):
        codec.private_field('public_' + str(i))
    assert codec._private_field.cache_info().currsize <= 4096
    before = codec._private_field.cache_info().currsize
    assert not codec.private_field('public_' + 'x' * 10000)
    assert codec._private_field.cache_info().currsize == before


def test_manifest_parses_json_once_and_keeps_packed_checkpoint(tmp_path, monkeypatch):
    path = fixture_bundle(tmp_path)
    original = json.loads
    calls = []
    def observe(value, *args, **kwargs):
        calls.append(1)
        return original(value, *args, **kwargs)
    monkeypatch.setattr(json, 'loads', observe)
    manifest = bundle.read_manifest(path)
    assert len(calls) == 1
    assert manifest['checkpoint']['exact'] == {'$type': 'decimal', 'value': '1234567890123456789.000000001'}


def test_group_validates_source_and_copy_once_and_returns_complete_counts(tmp_path, monkeypatch):
    first = fixture_bundle(tmp_path/'source', count=23)
    second = fixture_bundle(tmp_path/'source', sequence=2,
        previous=bundle.read_manifest(first)['bundle_id'], count=31)
    visited = Counter()
    original = folder.iter_bundle_rows
    def observe(path, **kwargs):
        kind = 'source' if str(path).startswith(str(tmp_path/'source')) else 'copy'
        for row in original(path, **kwargs):
            visited[kind] += 1
            yield row
    monkeypatch.setattr(folder, 'iter_bundle_rows', observe)
    counts = {}
    head = folder.FolderTransport(tmp_path/'transport').publish_group([first, second], row_counts=counts)
    assert head['sequence'] == 2
    assert visited == {'source': 54, 'copy': 54}
    assert counts == {'issuer': 54}


def test_failed_copy_leaves_counts_and_previous_head_unchanged(tmp_path, monkeypatch):
    first = fixture_bundle(tmp_path/'source')
    transport = folder.FolderTransport(tmp_path/'transport')
    head = transport.publish(first)
    second = fixture_bundle(tmp_path/'source', sequence=2, previous=head['bundle_id'])
    original = folder.shutil.copyfile
    def tamper(source, destination):
        result = original(source, destination)
        if str(destination).endswith('rows.ndjson.gz'):
            with open(destination, 'ab') as stream:
                stream.write(b'tampered')
        return result
    monkeypatch.setattr(folder.shutil, 'copyfile', tamper)
    counts = {}
    with pytest.raises(ValueError, match='hash'):
        transport.publish_group([second], row_counts=counts)
    assert counts == {}
    assert json.loads((tmp_path/'transport/heads/fixture.json').read_text()) == head


def test_changed_manifest_between_preflight_and_staging_never_advances_head(tmp_path, monkeypatch):
    path = fixture_bundle(tmp_path/'source')
    original = folder.iter_bundle_rows
    def replace_after_validation(source, **kwargs):
        yield from original(source, **kwargs)
        manifest = json.loads((path/'manifest.json').read_text())
        manifest['checkpoint'] = {'accessToken': 'hidden'}
        (path/'manifest.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(folder, 'iter_bundle_rows', replace_after_validation)
    counts = {}
    with pytest.raises(ValueError, match='manifest'):
        folder.FolderTransport(tmp_path/'transport').publish_group([path], row_counts=counts)
    assert counts == {} and not (tmp_path/'transport/heads').exists()


def test_corrupted_existing_destination_is_rejected_on_group_replay(tmp_path):
    path = fixture_bundle(tmp_path/'source')
    transport = folder.FolderTransport(tmp_path/'transport')
    head = transport.publish(path, advance_head=False)
    target = tmp_path/'transport/bundles'/head['bundle_id']/'rows.ndjson.gz'
    target.write_bytes(target.read_bytes() + b'tampered')
    counts = {}
    with pytest.raises(ValueError, match='hash'):
        transport.publish_group([path], row_counts=counts)
    assert counts == {} and not (tmp_path/'transport/heads').exists()


@pytest.mark.parametrize('limit', [{'max_new_rows': 3}, {'max_raw_bytes': 4}, {'max_requests': 1}, {}])
def test_hosted_volume_limit_finishes_current_response_without_claiming_next(monkeypatch, limit):
    from backtest_data.prices import krx_quote_collection as module
    from backtest_data.ingestion.jobs import JobLease
    from backtest_data import providers
    claims = []; calls = []; commits = []
    job = N(request={'service': 'kospi', 'session_date': '2026-10-07'}, checkpoint={'preserved': 'old'})
    @contextmanager
    def transaction(_):
        yield N(get=lambda *args: job)
    def acquire(*args, **kwargs):
        claims.append(1)
        return JobLease(str(len(claims)), 'same-owner', 7) if len(claims) <= 2 else None
    def request(*args):
        calls.append(1)
        return N(content=b'1234'), {}
    monkeypatch.setattr(module, 'transaction', transaction)
    monkeypatch.setattr(module, 'load_credentials', lambda **kwargs: N(krx_api_key=True))
    transport = N(close=lambda: None, request_budget=None)
    monkeypatch.setattr(module, 'ProviderHTTP', lambda _: transport)
    monkeypatch.setattr(providers, 'KRXData', lambda *args: N(daily_quotes=request))
    monkeypatch.setattr(module, 'acquire_job', acquire)
    monkeypatch.setattr(module, 'store_krx_quotes', lambda *args, **kwargs: {'inserted': 3, 'response_valid': True})
    monkeypatch.setattr(module, 'complete_job', lambda *args, **kwargs: commits.append((args[3], kwargs['status'])))
    monkeypatch.setattr(module, '_queue_counts', lambda *args: {'pending': 1, 'succeeded': 1})
    result = module.run_krx_quote_collection(None, 'unused', budget_seconds=6000, **limit)
    assert len(calls) == len(commits) == (1 if limit else 2)
    assert len(claims) == (1 if limit else 3)
    assert commits[0][0]['preserved'] == 'old' and commits[0][1] == 'succeeded'
    assert result['batch_limit_reached'] is bool(limit) and result['pending_requests'] == 1
    assert result['new_rows_this_run'] == (3 if limit else 6) and result['raw_bytes_this_run'] == (4 if limit else 8)
    assert result['blocked_sources'] == {} and result['coverage_complete'] is False
    if not limit:assert transport.request_budget is None
