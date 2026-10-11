from pathlib import Path

import pytest

from backtest_data.ingestion.raw_store import RawStore


def test_content_addressed_archive_is_idempotent_and_portable(tmp_path):
    store = RawStore(tmp_path)
    first = store.put(b"price revision one")
    assert first == store.put(b"price revision one")
    assert store.read(first.storage_key) == b"price revision one"
    assert not Path(first.storage_key).is_absolute()
    assert store.put(b"price revision two").content_hash != first.content_hash


def test_corrupted_or_missing_archive_is_rejected(tmp_path):
    store = RawStore(tmp_path)
    item = store.put(b"prices")
    (tmp_path / item.storage_key).write_bytes(b"broken")
    with pytest.raises(ValueError, match="integrity"):
        store.read(item.storage_key)
    with pytest.raises(ValueError, match="storage key"):
        store.read("../private.txt")
