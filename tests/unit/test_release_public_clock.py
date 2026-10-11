from datetime import timedelta

from test_financial_public_availability import CAPTURE, PUBLIC_BOUND, certificate, fixture_document
from backtest_data.fundamentals.models import FinancialSourceDocument, FundamentalFact, SourceFinancialFact
from backtest_data.releases.query_context import QueryContext, policy_bundle


def inputs():
    document, filing = fixture_document()
    document=FinancialSourceDocument(**vars(document))
    source=SourceFinancialFact(id='S',document_id=document.id,issuer_id='I',available_at=CAPTURE,
        captured_at=CAPTURE,extracted_at=CAPTURE,first_seen_at=CAPTURE,evidence={'source_node_ids':['N1']})
    mapping=FundamentalFact(id='M',issuer_id='I',source_fact_id='S',available_at=CAPTURE,
        captured_at=CAPTURE,materialized_at=CAPTURE)
    proof=certificate(document,filing)
    rows={'financial_source_document':{'D':document},'filing':{'F':filing},
        'source_financial_fact':{'S':source},'fundamental_fact':{'M':mapping},
        'financial_publication_evidence':{'E':proof}}
    return rows, source, mapping


def context(as_of=PUBLIC_BOUND,mode='reconstructed_public'):
    return QueryContext('R',as_of,mode,'fundamentals_sql','periodic_report',True)


def test_v6_source_and_mapping_use_public_original_clock_not_late_capture():
    from backtest_data.releases.public_clock import row_availability
    rows,source,mapping=inputs()
    for row in (source,mapping):
        before=row_availability(row,rows,context(PUBLIC_BOUND-timedelta(seconds=1)))
        after=row_availability(row,rows,context())
        assert before['status']=='unavailable'
        assert after['effective_available_at']==PUBLIC_BOUND
        assert after['evidence_ids']==['E']
    assert mapping.available_at==CAPTURE
    assert source.first_seen_at==CAPTURE


def test_observed_projection_keeps_actual_source_processing_clock():
    from backtest_data.releases.public_clock import row_availability
    rows,source,_=inputs()
    early=row_availability(source,rows,context(mode='observed_system'))
    assert early['status']=='unavailable'
    assert early['effective_available_at']==CAPTURE


def test_missing_frozen_certificate_does_not_consult_live_evidence():
    from backtest_data.releases.public_clock import row_availability
    rows,source,_=inputs()
    rows['financial_publication_evidence']={}
    assert row_availability(source,rows,context())['status']=='unavailable'
    assert row_availability(source,rows,context(CAPTURE))['status']=='capture_only'


def test_version_guard_keeps_legacy_visibility_unchanged():
    from backtest_data.releases.query import QueryRows, visible
    rows,source,_=inputs()
    v5=QueryRows(rows,{'policies':policy_bundle()})
    v6=QueryRows(rows,{'policies':policy_bundle('release_query_v6')})
    assert not visible(source,context(),v5)
    assert visible(source,context(),v6)
    assert not visible(source,context(mode='observed_system'),v6)


def test_v6_validation_uses_effective_inputs_and_v5_keeps_stored_clock(monkeypatch):
    from pathlib import Path
    from backtest_data.fundamentals.models import DerivedFact
    from backtest_data.releases.build import MODELS
    from backtest_data.releases.validation import validate_rows
    from backtest_data.releases import validation
    rows,source,mapping=inputs()
    for domain in MODELS:rows.setdefault(domain,{})
    metric=DerivedFact(id='T',entity_id='I',metric_id='revenue_ttm',value=100,status='ok',
        available_at=PUBLIC_BOUND,captured_at=CAPTURE,materialized_at=CAPTURE,evaluation_as_of=PUBLIC_BOUND,
        period={},basis={'public_availability_policy':'financial_public_pit_v1'},
        input_refs=[{'domain':'fundamental_fact','id':'M'},{'domain':'source_financial_fact','id':'S'}])
    rows['derived_fact']['T']=metric
    monkeypatch.setattr(validation,'registered_definition',lambda row:True)
    legacy=validate_rows(rows,Path('artifacts/tests/no-raw'))
    modern=validate_rows(rows,Path('artifacts/tests/no-raw'),policies=policy_bundle('release_query_v6'))
    assert any(item['reason']=='derived_future_input' for item in legacy)
    assert not any(item['reason']=='derived_future_input' for item in modern)
    metric.materialized_at=PUBLIC_BOUND
    bad=validate_rows(rows,Path('artifacts/tests/no-raw'),policies=policy_bundle('release_query_v6'))
    assert any(item['reason']=='derived_processing_backdated' for item in bad)


def test_metric_projection_pins_its_original_certificate_instead_of_later_proof():
    from datetime import date
    from backtest_data.fundamentals.models import DerivedFact
    from backtest_data.releases.public_clock import row_availability
    rows,source,mapping=inputs()
    document,filing=rows['financial_source_document']['D'],rows['filing']['F']
    earlier=certificate(document,filing,id='N',public_day=date(2024,2,29),bound=PUBLIC_BOUND-timedelta(days=1))
    rows['financial_publication_evidence']['N']=earlier
    metric=DerivedFact(id='T',basis={'public_availability_policy':'financial_public_pit_v1'},
        input_refs=[{'domain':'fundamental_fact','id':'M'},{'domain':'financial_publication_evidence','id':'E'}])
    result=row_availability(metric,rows,context(PUBLIC_BOUND-timedelta(seconds=1)))
    assert result['status']=='unavailable'
    assert result['effective_available_at']==PUBLIC_BOUND
    assert result['evidence_ids']==['E']


def test_latest_source_identity_uses_expanded_concepts_and_units():
    from backtest_data.releases.query import source_revision_identity
    original={'issuer_id':'I','concept':'g:Revenue','unit':'USD','dimensions':{},
        'period_kind':'duration','period_start':'2023-01-01','period_end':'2023-12-31',
        'evidence':{'concept_qname':'{http://fasb.org/us-gaap/2024}Revenue',
            'unit_measure_bindings':[{'role':'measure','measure_qname':'{iso4217}USD'}]}}
    correction={**original,'concept':'usgaap:Revenue'}
    assert source_revision_identity(original)==source_revision_identity(correction)


def test_v7_reuses_document_resolution_across_accounts_without_global_proof_scans(monkeypatch):
    from types import SimpleNamespace
    from backtest_data.releases.query import QueryRows
    from backtest_data.releases import public_clock
    rows,source,mapping=inputs()
    for number in range(1000):
        rows['financial_publication_evidence'][f'outside-{number}']=SimpleNamespace(
            document_id=f'other-{number}',status='qualified')
    frozen=QueryRows(rows,{'policies':policy_bundle('release_query_v7')})
    original=public_clock.resolve_document_availability
    inspected=[]
    def counted(document,filing,evidence_rows,**kwargs):
        evidence=list(evidence_rows);inspected.append(len(evidence))
        return original(document,filing,evidence,**kwargs)
    monkeypatch.setattr(public_clock,'resolve_document_availability',counted)
    for _ in range(20):
        assert public_clock.row_availability(source,frozen,context())['status']=='qualified'
        assert public_clock.row_availability(mapping,frozen,context())['status']=='qualified'
    assert inspected==[1]


def test_v7_cache_keeps_pinned_proof_scope_and_query_cutoffs_independent():
    from datetime import date
    from backtest_data.fundamentals.models import DerivedFact
    from backtest_data.releases.query import QueryRows
    from backtest_data.releases.public_clock import row_availability
    rows,source,_=inputs()
    document,filing=rows['financial_source_document']['D'],rows['filing']['F']
    earlier=certificate(document,filing,id='N',public_day=date(2024,2,29),bound=PUBLIC_BOUND-timedelta(days=1))
    rows['financial_publication_evidence']['N']=earlier
    original=DerivedFact(id='T',basis={'public_availability_policy':'financial_public_pit_v1'},
        input_refs=[{'domain':'source_financial_fact','id':'S'},
            {'domain':'financial_publication_evidence','id':'E'}])
    nested=DerivedFact(id='U',basis={'public_availability_policy':'financial_public_pit_v1'},
        input_refs=[{'domain':'derived_fact','id':'T'},
            {'domain':'financial_publication_evidence','id':'N'}])
    rows['derived_fact']={'T':original,'U':nested}
    frozen=QueryRows(rows,{'policies':policy_bundle('release_query_v7')})
    early=context(PUBLIC_BOUND-timedelta(seconds=1))
    assert row_availability(source,frozen,early)['status']=='qualified'
    assert row_availability(original,frozen,early)['status']=='unavailable'
    assert row_availability(original,frozen,context())['status']=='qualified'
    assert row_availability(nested,frozen,context())['status']=='unavailable'
    assert row_availability(source,frozen,early)['evidence_ids']==['N']
