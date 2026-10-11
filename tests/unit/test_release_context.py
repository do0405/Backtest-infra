from datetime import datetime, timezone
import pytest


def test_context_requires_explicit_release_cutoff_history_quality_and_disclosure():
    from backtest_data.releases.query_context import QueryContext
    valid=dict(release_id='R',as_of=datetime(2020,6,1,tzinfo=timezone.utc),history_mode='reconstructed_public',
               required_profile='fundamentals_sql',disclosure_profile='periodic_report',research_partial=True)
    assert QueryContext(**valid).release_id=='R'
    with pytest.raises(ValueError):
        QueryContext(**{**valid,'as_of':datetime(2020,6,1)})
    with pytest.raises(ValueError):
        QueryContext(**{**valid,'release_id':''})
    with pytest.raises(ValueError):
        QueryContext(**{**valid,'history_mode':'latest'})
    with pytest.raises(ValueError):
        QueryContext(**{**valid,'disclosure_profile':'not_applicable'})


def test_partial_override_never_bypasses_unsupported_policy_version():
    from backtest_data.releases.query_context import QueryContext, bind_context, policy_bundle
    context=QueryContext('R',datetime(2020,6,1,tzinfo=timezone.utc),'reconstructed_public','fundamentals_sql','periodic_report',True)
    manifest={'tier':'research_partial','policies':policy_bundle(), 'coverage':{'profiles':{'fundamentals_sql':False}}}
    assert bind_context(context,manifest)['query_version']=='release_query_v5'
    manifest['policies']['query_version']='unsupported'
    with pytest.raises(ValueError,match='unsupported'):
        bind_context(context,manifest)


def test_unknown_publication_evidence_uses_capture_as_conservative_public_floor():
    from backtest_data.fundamentals.models import Filing
    from backtest_data.releases.query import visible
    from backtest_data.releases.query_context import QueryContext
    march=datetime(2020,3,1,tzinfo=timezone.utc)
    june=datetime(2020,6,1,tzinfo=timezone.utc)
    context=QueryContext('R',datetime(2020,5,1,tzinfo=timezone.utc),'reconstructed_public','fundamentals_sql','periodic_report',True)
    filing=Filing(issuer_id='I',source='sec',accession='A',form='10-Q',published_at=None,
                  available_at=march,captured_at=june,evidence={'original_publication_verified':False})
    assert not visible(filing,context)
    filing.evidence={'original_publication_verified':True}
    filing.published_at=march
    assert visible(filing,context)


def test_v6_policy_is_explicit_and_v5_default_remains_unchanged():
    from backtest_data.releases.query_context import QueryContext, bind_context, policy_bundle
    context=QueryContext('R',datetime(2020,6,1,tzinfo=timezone.utc),'reconstructed_public','fundamentals_sql','periodic_report',True)
    legacy=policy_bundle()
    modern=policy_bundle('release_query_v6')
    assert legacy['query_version']=='release_query_v5'
    assert legacy['public_availability_policy']=='conservative_stored_clocks_v1'
    assert modern['public_availability_policy']=='financial_public_pit_v1'
    manifest={'tier':'research_partial','policies':modern,'coverage':{'profiles':{}}}
    assert bind_context(context,manifest)==modern
    modern['history_clock_policy']=legacy['history_clock_policy']
    with pytest.raises(ValueError,match='history_clock_policy'):
        bind_context(context,manifest)
