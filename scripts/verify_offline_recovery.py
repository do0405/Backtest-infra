"""Live fault injection in a separate canary stream; no provider or SQL calls."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['interrupt', 'recover', 'cleanup'])
    parser.add_argument('--repository', required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--gh', default='gh')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9]{8,24}', args.key): raise ValueError('unique canary key required')
    runtime = Path(os.environ['OFFLINE_RUNTIME_SOURCE']).resolve()
    sys.path[:0] = [str(runtime/'scripts'), str(runtime/'src')]
    import offline_publication as publication
    import offline_outbox as outbox
    from backtest_data.offline.transport import GitHubCaptureTransport
    from backtest_data.offline.archives import build_bundles
    from backtest_data.offline.contracts import make_capture
    stream = 'publication-canary-'+args.key
    catalog = sha256(('test-only-'+args.key).encode()).hexdigest()
    value = GitHubCaptureTransport(args.repository, gh=args.gh, workspace=args.work/args.phase/'transport',
                                  stream=stream, catalog_sha256=catalog)
    if args.phase == 'interrupt':
        raw = b'{"test_only":true,"bars":[]}'
        capture = make_capture(source='alpaca', kind='alpaca_bars',
            entity_id='00179df8-84f3-5ed8-9ea6-469a85bf94aa',
            captured_at=datetime.now(timezone.utc), content=raw, metadata={'test_only': True})
        state = {'catalog_sha256': catalog, 'sources': {}, 'provider_status': {}, 'test_only': True}
        original = publication.github
        def interrupted(gh, endpoint, **options):
            if '/assets?name=commit.json' in endpoint: raise publication.GitHubError(503, 'canary_injected_upload')
            return original(gh, endpoint, **options)
        publication.github = interrupted
        try: value.publish(build_bundles([(capture, raw)]), state)
        except publication.GitHubError as exc:
            if exc.operation != 'canary_injected_upload': raise
        else: raise AssertionError('injected interruption did not occur')
        pending = next(r for r in value._inventory() if r.get('tag_name', '').startswith('offline-pending-'+stream+'-'))
        canonical = next(r for r in value._inventory() if r.get('tag_name') == f'offline-{stream}-000000001')
        if {a['name'] for a in canonical['assets']} != {'state.json.gz', 'captures-0001.zip'}:
            # Read the actual server state, since the interrupted response did
            # not update the cached draft asset inventory.
            canonical = original(args.gh, f'repos/{args.repository}/releases/{canonical["id"]}')
        if {a['name'] for a in canonical['assets']} != {'state.json.gz', 'captures-0001.zip'}:
            raise AssertionError('missing-commit scenario was not reproduced')
        result = {'phase': 'interrupted', 'stream': stream, 'pending_id': pending['id'],
                  'commit_sha256': outbox.binding(pending, value)['commit_sha256'],
                  'canonical_id': canonical['id'], 'original_assets': canonical['assets']}
    elif args.phase == 'recover':
        restored = outbox.recover(value, on_commit=lambda commit, state: None)
        if len(restored) != 1: raise AssertionError('one saved publication must recover')
        commit = restored[0]
        with value.open_release(commit) as captures:
            items = list(captures)
        if len(items) != 1 or items[0][1] != b'{"test_only":true,"bars":[]}':
            raise AssertionError('exact canary capture was not preserved')
        if any(r.get('tag_name', '').startswith('offline-pending-'+stream+'-') for r in value._inventory()):
            raise AssertionError('verified temporary pending copy was not acknowledged')
        result = {'phase': 'recovered', 'stream': stream, 'commit_sha256': commit.sha256,
                  'canonical_id': commit.release['id'], 'capture_count': len(items),
                  'canonical_assets': commit.release['assets'], 'provider_requests': 0, 'sql_writes': 0}
    else:
        releases = [r for r in value._inventory() if r.get('tag_name') == f'offline-{stream}-000000001']
        if len(releases) != 1 or releases[0].get('draft'): raise ValueError('verified published canary required')
        commit = value._read_release(1, releases[0])
        with value.open_release(commit) as captures:
            if sum(1 for _ in captures) != 1: raise ValueError('exact test capture required')
        publication.github(args.gh, f'repos/{args.repository}/releases/{releases[0]["id"]}', method='DELETE')
        publication.github(args.gh, f'repos/{args.repository}/git/refs/tags/offline-{stream}-000000001', method='DELETE')
        result = {'phase': 'canary_cleaned', 'stream': stream, 'production_releases_modified': 0}
    args.work.mkdir(parents=True, exist_ok=True)
    (args.work/(args.phase+'-evidence.json')).write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__': main()
