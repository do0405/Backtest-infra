from types import SimpleNamespace

from backtest_data.cli import _parser, _dispatch


def test_archive_repair_cli_forwards_budget_and_does_not_requeue_partial(monkeypatch):
    import backtest_data.ops.kis_repair as repair
    args = _parser().parse_args(['maintenance','replay-kis-prices','--audit-path','audit.json',
        '--progress-path','progress.json','--budget-seconds','1','--requeue-recovered'])
    seen = []
    def replay(engine, root, path, **kwargs):
        seen.append((engine, root, path, kwargs)); return {'status':'partial'}
    monkeypatch.setattr(repair, 'replay_kis_price_audit', replay)
    monkeypatch.setattr(repair, 'requeue_recovered_kis_jobs', lambda *args: (_ for _ in ()).throw(
        AssertionError('partial recovery must never requeue')))
    result = _dispatch(args, 'engine', SimpleNamespace(raw_root='raw'))
    assert result['status'] == 'partial'
    assert seen == [('engine','raw','audit.json',{'progress_path':'progress.json','budget_seconds':1.0})]


def test_archive_repair_cli_requeues_only_complete_verified_receipt(monkeypatch):
    import backtest_data.ops.kis_repair as repair
    args = _parser().parse_args(['maintenance','replay-kis-prices','--audit-path','audit.json','--requeue-recovered'])
    receipt = {'status':'complete','audit_hash':'a'*64}
    monkeypatch.setattr(repair, 'replay_kis_price_audit', lambda *args, **kwargs: receipt)
    seen = []
    def requeue(*args):
        seen.append(args); return {'requeued_jobs':2}
    monkeypatch.setattr(repair, 'requeue_recovered_kis_jobs', requeue)
    result = _dispatch(args, 'engine', SimpleNamespace(raw_root='raw'))
    assert result['queue_recovery']['requeued_jobs'] == 2
    assert seen == [('engine','audit.json',receipt)]
