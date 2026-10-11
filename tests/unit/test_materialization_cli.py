from backtest_data.cli import _parser


def test_cli_exposes_sql_materialization_and_release_canonical_query():
    parser=_parser()
    normalized=parser.parse_args(['normalize','fundamentals'])
    assert normalized.batch_size==500
    derived=parser.parse_args(['derive','metrics','--as-of','2026-10-08T00:00:00Z',
                              '--history-mode','observed_system','--metric-id','growth_revenue_yoy'])
    assert derived.metric_id==['growth_revenue_yoy']
    query=parser.parse_args(['query','fundamentals','--release-id','release',
                            '--as-of','2026-10-08T00:00:00Z','--history-mode','observed_system'])
    assert query.action=='fundamentals'
    repaired=parser.parse_args(['maintenance','repair-dart-inventory'])
    assert repaired.action=='repair-dart-inventory'


def test_cli_exposes_live_and_frozen_alias_lookup_without_a_sample_limit():
    parser = _parser()
    seeded = parser.parse_args(['universe', 'sync-aliases'])
    assert seeded.market == 'both'
    assert not hasattr(seeded, 'limit')
    resolved = parser.parse_args(['resolve', 'A005930', '--market', 'kr'])
    assert resolved.identifier == 'A005930'
    assert resolved.history_mode == 'observed_system'
    frozen = parser.parse_args(['query', 'resolve', '--identifier', 'Samsung Electronics',
        '--market', 'kr', '--release-id', 'R', '--as-of', '2026-10-08T00:00:00Z',
        '--history-mode', 'observed_system', '--research-partial'])
    assert frozen.identifier == ['Samsung Electronics']
    prices = parser.parse_args(['query', 'prices', '--identifier', '005930', '--market', 'kr',
        '--release-id', 'R', '--as-of', '2026-10-08T00:00:00Z',
        '--history-mode', 'observed_system', '--research-partial'])
    assert prices.entity_id == []
    assert prices.identifier == ['005930']


def test_json_output_is_utf8_even_with_legacy_windows_stdout(monkeypatch):
    import io
    import json
    import sys
    from backtest_data.cli import _emit_json
    binary = io.BytesIO()
    stream = io.TextIOWrapper(binary, encoding='cp949')
    monkeypatch.setattr(sys, 'stdout', stream)
    expected = {'name':'삼성전자', 'alias':'🧪 Unicode'}
    _emit_json(expected)
    stream.flush()
    assert json.loads(binary.getvalue().decode('utf-8')) == expected


def test_daily_public_pit_cli_default_keeps_explicit_legacy_versions():
    parser=_parser()
    common=['derive','metrics','--as-of','2024-03-02T05:00:00Z','--history-mode','reconstructed_public']
    assert parser.parse_args(common).query_version=='release_query_v8_daily'
    assert parser.parse_args(common+['--query-version','release_query_v5']).query_version=='release_query_v5'
    assert parser.parse_args(common+['--query-version','release_query_v6']).query_version=='release_query_v6'
    release=parser.parse_args(['release','build','--manifest-id','M','--query-version','release_query_v6','--financial-scope','artifacts/scope.json'])
    assert release.financial_scope=='artifacts/scope.json'
    audit=parser.parse_args(['publication','audit','--manifest-id','M','--audit-path','artifacts/public-pit/audit.json'])
    assert audit.action=='audit'
    verify=parser.parse_args(['publication','verify','--audit-path','artifacts/public-pit/audit.json',
        '--progress-path','artifacts/public-pit/progress.json','--budget-seconds','60'])
    assert verify.batch_size==5
    assert verify.archive_compressed_mb==64 and verify.archive_total_mb==512
    larger=parser.parse_args(['publication','verify','--audit-path','artifacts/audit.json',
        '--archive-compressed-mb','256','--archive-total-mb','2048'])
    assert larger.archive_compressed_mb==256 and larger.archive_total_mb==2048
