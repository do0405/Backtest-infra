import json

import pytest


from scripts.collection_runtime import bind_runtime, extract_runtime


def test_runtime_upgrade_preserves_catalog_provider_gates_and_stream(tmp_path):
    original = {'runtime_tag': 'old', 'runtime_sha256': 'a'*64, 'catalog_tag': 'catalog',
                'catalog_sha256': 'c'*64, 'stream_id': 'core-forward-v1', 'dart_not_before': '2099-01-01'}
    pin = tmp_path / 'pin.json'
    pin.write_text(json.dumps({'format': 'offline_runtime_pin_v1',
                              'runtime_tag': 'offline-core-runtime-v6-20261031', 'runtime_sha256': 'b'*64}))
    updated = bind_runtime(original, 'core', pin)
    assert updated == {**original, 'runtime_tag': 'offline-core-runtime-v6-20261031', 'runtime_sha256': 'b'*64}
    assert original['runtime_tag'] == 'old'


@pytest.mark.parametrize('pin', [{}, {'format': 'offline_runtime_pin_v1', 'runtime_tag': 'arbitrary', 'runtime_sha256': 'b'*64},
                                 {'format': 'offline_runtime_pin_v1', 'runtime_tag': 'offline-core-runtime-v6-20261031', 'runtime_sha256': ''}])
def test_invalid_or_partial_pin_is_rejected(tmp_path, pin):
    path = tmp_path / 'pin.json'
    path.write_text(json.dumps(pin))
    with pytest.raises(ValueError): bind_runtime({}, 'core', path)


def test_missing_pin_cannot_fall_back_to_a_stale_private_runtime(tmp_path):
    with pytest.raises(FileNotFoundError):
        bind_runtime({'runtime_tag': 'existing'}, 'core', tmp_path/'absent')



@pytest.mark.parametrize(('member', 'reason'), [
    ('../outside.py', 'unsafe runtime archive path'),
    ('payload.dat', 'code-only runtime required'),
])
def test_runtime_rejects_non_code_or_unsafe_members_even_after_hash_verification(tmp_path, member, reason):
    from hashlib import sha256
    from zipfile import ZipFile

    archive = tmp_path/'runtime.zip'
    with ZipFile(archive, 'w') as target:
        target.writestr(member, b'test data')
    destination = tmp_path/'source'
    with pytest.raises(ValueError, match=reason):
        extract_runtime(archive, destination, sha256(archive.read_bytes()).hexdigest())
    assert not destination.exists()
