import json
from pathlib import Path


def host(tmp_path):
    from backtest_data.ops.continuation_host import WindowsHost
    h=WindowsHost(tmp_path,tmp_path/'helper','powershell');h.ready=True
    return h


def test_unregistered_matching_collector_is_adopted_as_existing_work(tmp_path):
    h=host(tmp_path)
    h.processes={99:{'ProcessId':99,'ParentProcessId':90,'CommandLine':
        'python -m backtest_data collect --market us --domain prices --source alpaca'}}
    assert h.observe({'pid':1,'source_group':'alpaca_prices'}, {})=='orphan'


def test_other_supplier_lane_does_not_block_a_separate_role(tmp_path):
    h=host(tmp_path)
    h.processes={99:{'ProcessId':99,'ParentProcessId':90,'CommandLine':
        'python -m backtest_data collect --market us --domain prices --source alpaca'}}
    assert h.observe({'pid':1,'source_group':'prices'}, {})=='gone'


def test_saved_wrapper_identity_mismatch_withholds_launch(tmp_path):
    h=host(tmp_path);identity=tmp_path/'identity.json'
    identity.write_text(json.dumps({'ProcessId':1,'CreationDate':'old','CommandLine':'old'}))
    h.processes={1:{'ProcessId':1,'CreationDate':'new','CommandLine':'new'}}
    assert h.observe({'pid':1,'process_identity_path':str(identity)}, {})=='identity_mismatch'


def test_inventory_failure_never_means_worker_is_absent(tmp_path):
    h=host(tmp_path);h.ready=False
    assert h.observe({'pid':1},{})=='unknown'
