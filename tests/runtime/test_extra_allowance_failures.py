import importlib.util
import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def cycle(monkeypatch):
    source=os.environ.get('EXTRA_RUNTIME_SOURCE')
    if not source:pytest.skip('EXTRA_RUNTIME_SOURCE required')
    runtime=Path(source)
    monkeypatch.syspath_prepend(str(runtime/'src'))
    monkeypatch.syspath_prepend(str(runtime/'scripts'))
    spec=importlib.util.spec_from_file_location('extra_allowance_test',runtime/'scripts/remote_extended_cycle.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_temporary_allowance_failure_disables_only_that_source_without_refunding(cycle,monkeypatch,capsys):
    from backtest_data.remote.releases import GitHubOperationError
    calls=[]
    def reserve(*args,**kwargs):
        calls.append(kwargs['source'])
        if kwargs['source']=='alpha_vantage':raise GitHubOperationError(503,'runtime_read')
        return {'credits':10}
    monkeypatch.setattr(cycle,'reserve_allowance',reserve)
    assert cycle.reserve_source_allowance(source='alpha_vantage') is None
    assert cycle.reserve_source_allowance(source='fsc_datago')=={'credits':10}
    details=json.loads(capsys.readouterr().out)
    assert details=={'status':'extra_allowance_deferred','source':'alpha_vantage','http_status':503,'operation':'runtime_read'}
    assert calls==['alpha_vantage','fsc_datago']


def test_authentication_and_integrity_failures_remain_visible(cycle,monkeypatch):
    from backtest_data.remote.releases import GitHubAccessError
    for error in (GitHubAccessError(401,'runtime_read'),ValueError('burned source quota readback mismatch')):
        def reserve(*args,**kwargs):raise error
        monkeypatch.setattr(cycle,'reserve_allowance',reserve)
        with pytest.raises(type(error)):
            cycle.reserve_source_allowance(source='alpha_vantage')
