import importlib.util
import json
from pathlib import Path

import pytest


def module():
    source = Path(__file__).resolve().parents[2] / 'scripts/offline_runtime_launcher.py'
    spec = importlib.util.spec_from_file_location('runtime_pin_test', source)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_runtime_upgrade_preserves_catalog_provider_gates_and_stream(tmp_path):
    original = {'runtime_tag': 'old', 'runtime_sha256': 'a'*64, 'catalog_tag': 'catalog',
                'catalog_sha256': 'c'*64, 'stream_id': 'core-forward-v1', 'dart_not_before': '2099-01-01'}
    pin = tmp_path / 'pin.json'
    pin.write_text(json.dumps({'format': 'offline_runtime_pin_v1',
                              'runtime_tag': 'offline-core-runtime-v6-20261031', 'runtime_sha256': 'b'*64}))
    updated = module().runtime_binding(original, pin)
    assert updated == {**original, 'runtime_tag': 'offline-core-runtime-v6-20261031', 'runtime_sha256': 'b'*64}
    assert original['runtime_tag'] == 'old'


@pytest.mark.parametrize('pin', [{}, {'format': 'offline_runtime_pin_v1', 'runtime_tag': 'arbitrary', 'runtime_sha256': 'b'*64},
                                 {'format': 'offline_runtime_pin_v1', 'runtime_tag': 'offline-core-runtime-v6-20261031', 'runtime_sha256': ''}])
def test_invalid_or_partial_pin_is_rejected(tmp_path, pin):
    path = tmp_path / 'pin.json'
    path.write_text(json.dumps(pin))
    with pytest.raises(ValueError): module().runtime_binding({}, path)


def test_missing_public_pin_keeps_existing_private_configuration(tmp_path):
    original = {'runtime_tag': 'existing'}
    assert module().runtime_binding(original, tmp_path/'absent') is original
