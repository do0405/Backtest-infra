import hashlib
import json

import pytest

from backtest_data.releases.backup import verify_backup


def test_backup_verification_detects_corruption_and_path_escape(tmp_path):
    dump = tmp_path / "database.dump"
    dump.write_bytes(b"PGDMP-test")
    manifest = {"format_version": "dataset_backup_v1", "database_dump": "database.dump", "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(), "raw_objects": [], "snapshot_consistent": True, "disaster_independent": False}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    assert verify_backup(path)["integrity_verified"] is True
    dump.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        verify_backup(path)
    manifest["database_dump"] = "../elsewhere.dump"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="path"):
        verify_backup(path)


def test_backup_verification_requires_every_referenced_raw_object(tmp_path):
    dump = tmp_path / "database.dump"
    dump.write_bytes(b"PGDMP-test")
    manifest = {"format_version": "dataset_backup_v1", "database_dump": "database.dump", "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(), "raw_objects": [{"storage_key": "sha256/ab/missing.gz", "content_hash": "0" * 64, "byte_count": 1}], "snapshot_consistent": True, "disaster_independent": False}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Archive integrity"):
        verify_backup(path)
