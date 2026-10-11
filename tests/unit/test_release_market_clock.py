from datetime import date, datetime, timedelta, timezone

import pytest

from backtest_data.prices.models import PriceObservation
from backtest_data.releases.query import QueryRows, visible
from backtest_data.releases.query_context import QueryContext, policy_bundle
from test_release_public_clock import inputs, context as financial_context
from test_financial_public_availability import CAPTURE, PUBLIC_BOUND


def fixture(market='KR', basis='as_traded'):
    row=PriceObservation(id='P',security_id='S',session=date(2024,3,1),basis=basis,
        available_at=CAPTURE,captured_at=CAPTURE,evidence={'availability_precision':'capture_only'})
    rows=QueryRows({'price_observation':{'P':row}}, {'policies':policy_bundle('release_query_v7'),
        'catalog':{'securities':{'S':{'market':market}}}})
    return row,rows


def cutoff(value,mode='reconstructed_public'):
    return QueryContext('R',value,mode,'core_price_volume','not_applicable',True)


@pytest.mark.parametrize(('market','bound'),[
    ('KR',datetime(2024,3,1,15,tzinfo=timezone.utc)),
    ('US',datetime(2024,3,2,5,tzinfo=timezone.utc)),
])
def test_reconstructed_price_uses_completed_local_day_not_capture(market,bound):
    price,rows=fixture(market)
    assert not visible(price,cutoff(bound-timedelta(seconds=1)),rows)
    assert visible(price,cutoff(bound),rows)
    assert price.available_at==CAPTURE and price.captured_at==CAPTURE
    assert not visible(price,cutoff(bound,'observed_system'),rows)


def test_unknown_market_and_adjusted_series_fail_closed():
    price,rows=fixture('UNKNOWN')
    assert not visible(price,cutoff(CAPTURE),rows)
    price,rows=fixture(basis='split_adjusted')
    assert not visible(price,cutoff(CAPTURE),rows)


def test_v7_financials_require_certification_even_after_capture():
    values,source,_=inputs()
    values['financial_publication_evidence']={}
    rows=QueryRows(values,{'policies':policy_bundle('release_query_v7')})
    assert not visible(source,financial_context(CAPTURE),rows)


def test_v7_preserves_qualified_original_public_clock():
    values,source,_=inputs()
    rows=QueryRows(values,{'policies':policy_bundle('release_query_v7')})
    assert not visible(source,financial_context(PUBLIC_BOUND-timedelta(seconds=1)),rows)
    assert visible(source,financial_context(PUBLIC_BOUND),rows)


def test_numeric_public_certificate_cannot_backdate_different_fiscal_metadata():
    values,source,mapping=inputs()
    document=values['financial_source_document']['D']
    mapping.basis={'sec_fiscal_metadata':{'focus':'Q1'},'source_document_hash':document.content_hash}
    proof=values['financial_publication_evidence']['E']
    assert proof.evidence['proof']['original_body_hash']!=document.content_hash
    rows=QueryRows(values,{'policies':policy_bundle('release_query_v7')})
    from backtest_data.releases.public_clock import row_availability
    assert row_availability(mapping,rows,financial_context(PUBLIC_BOUND))['status']=='qualified'
    assert not visible(mapping,financial_context(PUBLIC_BOUND),rows)


def test_v5_and_v6_price_clocks_keep_original_semantics():
    price,_=fixture()
    for version in ('release_query_v5','release_query_v6'):
        rows=QueryRows({}, {'policies':policy_bundle(version)})
        assert not visible(price,cutoff(PUBLIC_BOUND),rows)


def test_price_scope_is_explicit_and_preserves_original_denominator():
    from types import SimpleNamespace
    from backtest_data.releases.build import price_scope_descriptor
    target=SimpleNamespace(id='T',content_hash='H',members=[{},{}])
    result=price_scope_descriptor(target,{'start':date(2024,3,1),'end':date(2024,3,2)})
    assert result['original_target_count']==2
    assert result['kind']=='price_sessions_v1'
    assert 'fundamentals_sql' in result['excluded_profiles']
    with pytest.raises(ValueError):price_scope_descriptor(target,{'start':date(2024,3,2),'end':date(2024,3,1)})
