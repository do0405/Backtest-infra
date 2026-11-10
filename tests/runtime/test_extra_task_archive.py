"""Compatibility and integrity checks for the isolated extra runtime."""
import gzip
from hashlib import sha256
import importlib
import os
from pathlib import Path
from uuid import UUID

import pytest


@pytest.fixture
def archive(monkeypatch):
    source = os.environ.get('EXTRA_RUNTIME_SOURCE')
    if not source:
        pytest.skip('EXTRA_RUNTIME_SOURCE must point to the extracted extra runtime')
    monkeypatch.syspath_prepend(str(Path(source) / 'src'))
    module = importlib.import_module('backtest_data.remote.task_archive')
    assert Path(module.__file__).is_relative_to(Path(source).resolve())
    return module


@pytest.fixture
def binding():
    return dict(stream_id='extra-forward-v1', start_sequence=684, manifest_id='fixture',
                target_hash='a' * 64, target_count=17758, output_start='2016-01-01',
                output_end='2026-10-07', forward_start='2026-10-08', owned_sources=['kis'])


def state():
    return dict(checkpoint_marker='preserved', task_state=[],
                completed_task_ids=[f'{i:064x}' for i in range(12)],
                unfinished_task_ids=[f'{i:064x}' for i in range(12, 17)],
                price_history_queue={'kis': [str(UUID(int=2)), str(UUID(int=1))]})


def test_sharded_round_trip_exceeds_legacy_expansion_bound(archive, binding, tmp_path, monkeypatch):
    monkeypatch.setattr(archive, 'MAX_EXPANDED_BYTES', 600)
    monkeypatch.setattr(archive, 'MAX_SHARD_EXPANDED_BYTES', 550, raising=False)
    original = state()
    root = tmp_path / 'output'
    checkpoint = archive.detach_checkpoint(original, root, binding=binding)
    ref = checkpoint['task_state_archive']
    assert len(ref['shards']) > 1
    assert sum(shard['expanded_bytes'] for shard in ref['shards']) > 600
    assert all(shard['expanded_bytes'] <= 550 for shard in ref['shards'])
    assert archive.hydrate_checkpoint(checkpoint, root, binding=binding) == original
    resume = tmp_path / 'resume'
    archive.stage_archive(ref, root, resume, binding=binding)
    from backtest_data.remote.archives import restore
    from backtest_data.remote.releases import zip_store
    zip_path = tmp_path / 'resume.zip'
    zip_store(resume, zip_path)
    restored = tmp_path / 'restored'
    restore(zip_path, restored, 2 * 1024**3)
    assert archive.hydrate_checkpoint(checkpoint, restored, binding=binding) == original


def test_legacy_checkpoint_remains_readable(archive, binding, tmp_path):
    identity = 'b' * 64
    format_v1 = 'backtest_portable_task_state_ndjson_v1'
    path = 'heads/task-state/metadata.json.gz'
    wire = ''.join(archive.dumps(row) + '\n' for row in (
        dict(format=format_v1, binding=binding, present_fields=['completed_task_ids']),
        dict(kind='completed', value=identity),
    )).encode()
    compressed = gzip.compress(wire, mtime=0)
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_bytes(compressed)
    ref = dict(format=format_v1, path=path, binding=binding, bytes=len(compressed),
               expanded_bytes=len(wire), sha256=sha256(compressed).hexdigest(),
               payload_sha256=sha256(wire).hexdigest(),
               counts={kind: int(kind == 'completed') for kind in archive.KINDS}, source_counts={})
    assert archive.hydrate_checkpoint(dict(task_state_archive=ref), tmp_path, binding=binding) == {
        'completed_task_ids': [identity]}


def test_tampered_shard_does_not_reach_resume_store(archive, binding, tmp_path, monkeypatch):
    monkeypatch.setattr(archive, 'MAX_SHARD_EXPANDED_BYTES', 550, raising=False)
    source = tmp_path / 'source'
    ref = archive.detach_checkpoint(state(), source, binding=binding)['task_state_archive']
    path = source / ref['shards'][-1]['path']
    path.write_bytes(path.read_bytes() + b'x')
    with pytest.raises(ValueError, match='byte proof'):
        archive.stage_archive(ref, source, tmp_path / 'target', binding=binding)
    assert not list((tmp_path / 'target').rglob('*.gz'))


def test_shard_manifest_cannot_reorder_records(archive, binding, tmp_path, monkeypatch):
    monkeypatch.setattr(archive, 'MAX_SHARD_EXPANDED_BYTES', 550)
    checkpoint = archive.detach_checkpoint(state(), tmp_path, binding=binding)
    checkpoint['task_state_archive']['shards'].reverse()
    with pytest.raises(ValueError, match='reference required'):
        archive.hydrate_checkpoint(checkpoint, tmp_path, binding=binding)
