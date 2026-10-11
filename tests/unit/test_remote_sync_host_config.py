import importlib.util
import os
from pathlib import Path
import pytest


def module():
    path=Path(__file__).resolve().parents[2]/'scripts/local_sync_daemon.py'
    spec=importlib.util.spec_from_file_location('local_sync_host',path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


def test_existing_cli_directory_is_bound_without_reading_credentials(tmp_path,monkeypatch):
    monkeypatch.delenv('GH_CONFIG_DIR',raising=False)
    module().configure_github({'github_config_directory':str(tmp_path)})
    assert os.environ['GH_CONFIG_DIR']==str(tmp_path)
    assert list(tmp_path.iterdir())==[]


def test_missing_cli_directory_fails_closed(tmp_path):
    with pytest.raises(ValueError,match='existing_github_configuration_required'):
        module().configure_github({'github_config_directory':str(tmp_path/'absent')})
