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


def test_release_download_paths_fit_windows_raw_blob_layout(tmp_path,monkeypatch):
    value=module();tag='data-krx-live-fc021362-000000003-r38115965869-a1'
    root=Path('E:/side project/Backtest infra/artifacts/remote-sync/krx-live-fc021362')
    monkeypatch.setattr(value,'inventory',lambda *args:[{'tag_name':tag}])
    monkeypatch.setattr(value.shutil,'disk_usage',lambda _:type('Disk',(),{'free':100*1024**3})())
    def capture(gh,repo,release,name,directory,**kwargs):
        raw=directory/'store/bundles/11abac06-5908-4dbc-819d-82616eae0a9e/raw/sha256/f4'/('f'*64+'.gz')
        assert len(str(raw))<260
        raise StopIteration
    monkeypatch.setattr(value,'download_asset',capture)
    with pytest.raises(StopIteration):
        value.sync_once(None,'gh',{'repository':'do0405/Backtest-infra','owner_id':'krx-live-fc021362','bootstrap':{}},root,{})
