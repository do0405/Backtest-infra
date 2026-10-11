from dataclasses import replace
from backtest_research.contracts import AnalysisDataset
from backtest_research.screening import screen_subset
from tests.oracles.research_ledger import dataset


def merge(*datasets):
    sample = datasets[0]
    return AnalysisDataset.create(bars=tuple(b for d in datasets for b in d.bars), sessions=sample.sessions,
        cutoff=sample.cutoff, manifest_id=sample.manifest_id, manifest_hash=sample.manifest_hash,
        original_target_count=17758, source_snapshot_at=sample.source_snapshot_at,
        quality_gaps=sample.quality_gaps, origin='test_fixture', calendar_grade=sample.calendar_grade)


def test_stable_ties_and_unavailable_are_retained_with_full_denominator():
    data = merge(dataset([10, 11, 12, 13], security='B'), dataset([10, 11, 12, 13], security='A'),
                 dataset([10, 11, 12, 13], security='C', missing=(2,)))
    result = screen_subset(data, feature='momentum', parameters={'momentum_lookback': 3, 'momentum_skip': 1}, top_n=1)
    assert [row['security_id'] for row in result['rows']] == ['A']
    assert result['eligible_count'] == 2
    assert result['excluded_count'] == 1
    assert result['excluded'][0]['reason'] == 'missing_session'
    assert result['original_target_count'] == 17758
    assert result['subset_only'] is True
    assert result['historical_universe_verified'] is False


def test_adjustment_definition_is_not_inferred_and_ambiguous_sources_excluded():
    raw = dataset([10, 11, 12], security='A')
    adjusted = dataset([10, 11, 12], security='A', basis='provider_adjusted', adjustments=('dividend',))
    result = screen_subset(merge(raw, adjusted), basis='split_only', feature='price')
    assert not result['rows']
    assert result['excluded'][0]['reason'] == 'requested_basis_unavailable'
    assert screen_subset(merge(raw, adjusted), basis='as_traded', feature='price')['rows'][0]['score'] == '12'
    alternative = replace(raw.bars[0].series, source_contract_id='other')
    duplicate = AnalysisDataset.create(bars=raw.bars + tuple(replace(b, series=alternative) for b in raw.bars),
        sessions=raw.sessions, cutoff=raw.cutoff, manifest_id=raw.manifest_id, manifest_hash=raw.manifest_hash,
        original_target_count=17758, source_snapshot_at=raw.source_snapshot_at, quality_gaps=(), origin='test_fixture')
    assert screen_subset(duplicate, feature='price')['excluded'][0]['reason'] == 'ambiguous_series_selection'
