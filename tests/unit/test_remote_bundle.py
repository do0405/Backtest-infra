from datetime import date, datetime, timezone
from decimal import Decimal
import importlib
import json

import pytest


NOW = datetime(2026, 10, 10, 5, tzinfo=timezone.utc)
SCOPE = {"manifest_id": "whole-target", "target_count": 17758, "output_start": "2016-01-01", "output_end": "2026-10-07"}


def remote(name):
    try:
        spec = importlib.util.find_spec("backtest_data.remote." + name)
    except ModuleNotFoundError:
        spec = None
    assert spec is not None, "remote delta groundwork is required"
    return importlib.import_module("backtest_data.remote." + name)


def row():
    return {"table": "issuer", "values": {"id": "issuer-1", "market": "US", "source_identifier": "0000123456", "name": "Historical Issuer"}}


def bundle(tmp_path, **kwargs):
    return remote("bundle").write_bundle(tmp_path, [row()], stream_id="sec-financial", sequence=1,
        scope=SCOPE, created_at=NOW, checkpoint={"completed_task_ids": []}, **kwargs)


def test_codec_preserves_exact_decimals_and_original_utc_clocks():
    mod = remote("codec")
    source = {"v": Decimal("123456789012345678901234.123456789012"), "captured_at": NOW, "session": date(2016, 1, 4), "id": "0000123456"}
    assert mod.loads(mod.dumps(source)) == source
    with pytest.raises(ValueError, match="float"):
        mod.dumps({"v": 0.1})
    with pytest.raises(ValueError, match="aware"):
        mod.dumps({"captured_at": datetime(2026, 10, 10)})


def test_public_financial_concept_names_are_not_operational_lease_fields():
    mod=remote('codec')
    concepts={'OperatingLeaseCost':{'value':'123'},'CommonStockSharesAuthorized':{'value':'1000'}}
    assert mod.loads(mod.dumps(concepts))==concepts
    with pytest.raises(ValueError,match='private'):mod.dumps({'accessToken':'private'})


def test_jsonb_numeric_metadata_roundtrips_as_exact_json_numbers():
    from backtest_data.remote.registry import exact_json_text
    original={'minimum_interval_seconds':Decimal('0.31'),'large':Decimal('12345678901234567890.12345678901234567890'),
              'cursor':'page-100','valid':True,'missing':None}
    encoded=exact_json_text(original)
    assert json.loads(encoded,parse_float=Decimal)==original
    assert '"0.31"' not in encoded
    with pytest.raises(ValueError,match='float'):exact_json_text({'approximate':0.31})


@pytest.mark.parametrize("value", [{"headers": {"Authorization": "hidden"}}, {"checkpoint": {"next_page_token": "opaque"}}, {"lease_owner": "local"}, {"database_url": "hidden"}])
def test_private_state_never_enters_payload(value):
    with pytest.raises(ValueError, match="private"):
        remote("codec").dumps(value)


def test_bundle_manifest_hashes_and_whole_scope_round_trip(tmp_path):
    path = bundle(tmp_path)
    mod = remote("bundle")
    manifest = mod.read_manifest(path)
    assert manifest["scope"] == SCOPE
    assert manifest["files"][0]["row_count"] == 1
    assert list(mod.iter_bundle_rows(path)) == [row()]
    assert manifest["checkpoint"] == {"completed_task_ids": []}


def test_manifest_shape_and_private_state_are_validated_from_same_bytes(tmp_path,monkeypatch):
    from pathlib import Path
    path=bundle(tmp_path);file=path/'manifest.json'
    clean=file.read_text(encoding='utf-8')
    value=json.loads(clean);value['checkpoint']={'database_url':'hidden'}
    changed=json.dumps(value);file.write_text(changed,encoding='utf-8')
    original=Path.read_text;reads=0
    def alternating(self,*args,**kwargs):
        nonlocal reads
        if self==file:
            reads+=1
            return changed if reads==1 else clean
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',alternating)
    with pytest.raises(ValueError,match='private'):
        remote('bundle').read_manifest(path)


def test_bundle_tamper_and_path_traversal_are_rejected(tmp_path):
    path = bundle(tmp_path)
    payload = path / "rows.ndjson.gz"
    payload.write_bytes(payload.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="hash"):
        list(remote("bundle").iter_bundle_rows(path))
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["files"][0]["path"] = "../outside.gz"
    (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="path"):
        remote("bundle").read_manifest(path)


def test_sql_and_operational_tables_are_not_an_import_format(tmp_path):
    bad = {"table": "collection_job", "values": {"id": "job", "request": {}}}
    with pytest.raises(ValueError, match="allowlist"):
        remote("bundle").write_bundle(tmp_path, [bad], stream_id="s", sequence=1, scope=SCOPE, created_at=NOW)


def test_folder_publish_then_pull_preserves_manifest_and_never_deletes_unreceived_data(tmp_path):
    path = bundle(tmp_path / "producer")
    folder = remote("folder").FolderTransport(tmp_path / "private-store")
    head = folder.publish(path)
    pulled = folder.pull(head["bundle_id"], tmp_path / "consumer", expected_manifest_hash=head["manifest_hash"])
    assert list(remote("bundle").iter_bundle_rows(pulled)) == [row()]
    assert folder.publish(path) == head
    assert (tmp_path / "private-store" / "bundles" / head["bundle_id"]).is_dir()


def test_remote_ownership_is_disabled_and_whole_universe_is_not_reduced():
    mod = remote("planner")
    with pytest.raises(ValueError, match="disabled"):
        mod.validate_ownership({"enabled": False, "provider_slots": []})
    with pytest.raises(ValueError, match="local"):
        mod.validate_ownership({"enabled": True, "owner_id": "remote", "provider_slots": [{"source": "sec", "account_id": "sec-account", "owner": "remote", "local_enabled": True, "market": "us", "domain": "fundamentals"}]})
    plan = mod.refresh_plan(SCOPE, as_of=NOW, recent_sessions=[date(2026, 10, 6), date(2026, 10, 7)])
    assert plan["scope"] == SCOPE
    assert plan["target_count"] == 17758
    assert plan["coverage_complete"] is False


def test_chunk_group_advances_head_only_after_every_chunk_is_published(tmp_path,monkeypatch):
    from backtest_data.remote.bundle import write_bundle,read_manifest
    from backtest_data.remote.folder import FolderTransport
    first=write_bundle(tmp_path/'producer',[row()],stream_id='chunks',sequence=1,scope=SCOPE)
    folder=FolderTransport(tmp_path/'private');original=folder.publish(first)
    second=write_bundle(tmp_path/'producer',[],stream_id='chunks',sequence=2,scope=SCOPE,previous_bundle_id=original['bundle_id'])
    third=write_bundle(tmp_path/'producer',[],stream_id='chunks',sequence=3,scope=SCOPE,previous_bundle_id=read_manifest(second)['bundle_id'],checkpoint={'finished':True})
    import backtest_data.remote.folder as module
    copy=module.shutil.copyfile
    def fail_last(source,destination):
        if str(read_manifest(third)['bundle_id']) in str(destination):raise OSError('interrupted final chunk')
        return copy(source,destination)
    monkeypatch.setattr(module.shutil,'copyfile',fail_last)
    with pytest.raises(OSError):folder.publish_group([second,third])
    assert json.loads((tmp_path/'private'/'heads'/'chunks.json').read_text())==original
    assert folder.committed_bundles()==[tmp_path/'private'/'bundles'/original['bundle_id']]
    monkeypatch.setattr(module.shutil,'copyfile',copy)
    completed=folder.publish_group([second,third])
    assert completed['sequence']==3
    assert [read_manifest(path)['sequence'] for path in folder.committed_bundles()]==[1,2,3]


def test_chunk_group_storage_cap_preserves_previous_head_without_partial_copy(tmp_path):
    from backtest_data.remote.bundle import write_bundle,read_manifest
    from backtest_data.remote.folder import FolderTransport
    first=write_bundle(tmp_path/'producer',[row()],stream_id='chunks',sequence=1,scope=SCOPE)
    folder=FolderTransport(tmp_path/'private');original=folder.publish(first)
    second=write_bundle(tmp_path/'producer',[],stream_id='chunks',sequence=2,scope=SCOPE,previous_bundle_id=original['bundle_id'])
    folder.max_retained_bytes=1
    with pytest.raises(ValueError,match='storage'):folder.publish_group([second])
    assert json.loads((tmp_path/'private'/'heads'/'chunks.json').read_text())==original
    assert not (tmp_path/'private'/'bundles'/read_manifest(second)['bundle_id']).exists()
