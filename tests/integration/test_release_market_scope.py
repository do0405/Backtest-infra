from datetime import date,datetime,timezone

import pytest

from test_release_queries import seeded
from backtest_data.releases.build import build_release
from backtest_data.releases.query import get_prices,get_source_accounts
from backtest_data.releases.query_context import QueryContext

pytestmark=pytest.mark.integration


def test_v7_price_scope_freezes_all_targets_and_boundary_without_financials(seeded):
    release=build_release(seeded['session'],seeded['target'].id,raw_root=seeded['raw_root'],
        query_version='release_query_v7',price_scope={'start':date(2020,2,28),'end':date(2020,2,28)})
    assert release['target_count']==2
    assert release['counts']['source_financial_fact']==0
    assert release['counts']['price_observation']==1
    assert release['research_scope']['original_target_count']==2
    def ctx(value,mode='reconstructed_public',financial=False):
        return QueryContext(release['release_id'],value,mode,
            'fundamentals_sql' if financial else 'core_price_volume',
            'periodic_report' if financial else 'not_applicable',True)
    before=datetime(2020,2,29,4,59,59,tzinfo=timezone.utc)
    after=datetime(2020,2,29,5,tzinfo=timezone.utc)
    assert not get_prices(seeded['session'],[],ctx(before))['rows']
    result=get_prices(seeded['session'],[],ctx(after))
    assert len(result['rows'])==1
    assert result['rows'][0]['market_availability']['acquisition_pit_required'] is False
    assert not get_prices(seeded['session'],[],ctx(after,'observed_system'))['rows']
    with pytest.raises(ValueError,match='outside_release_scope'):
        get_source_accounts(seeded['session'],[],ctx(after,financial=True))


def test_price_scope_cannot_silently_load_outside_declared_sessions(seeded):
    from backtest_data.releases.models import ValidationIssue
    seeded['session'].add(ValidationIssue(record_domain='price_observation',record_id=seeded['price'].id,
        rule='fixture_outside_scope',severity='warning',evidence={'security_id':seeded['security'].id}))
    seeded['session'].flush()
    release=build_release(seeded['session'],seeded['target'].id,raw_root=seeded['raw_root'],
        query_version='release_query_v7',price_scope={'start':date(2020,3,1),'end':date(2020,3,2)})
    assert release['counts']['price_observation']==0
    assert release['target_count']==2


def test_price_scope_requires_v7_and_does_not_reinterpret_old_versions(seeded):
    with pytest.raises(ValueError,match='exclusive_v7'):
        build_release(seeded['session'],seeded['target'].id,raw_root=seeded['raw_root'],
            query_version='release_query_v6',price_scope={'start':date(2020,2,28),'end':date(2020,2,28)})
