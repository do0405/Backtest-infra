from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
import pytest

from backtest_data.fundamentals.models import DerivedFact, Filing, FinancialSourceDocument, FundamentalFact, SourceFinancialFact
from backtest_data.releases.build import MODELS
from backtest_data.releases.models import SelectionDecision
from backtest_data.releases.query_context import QueryContext


MARCH=datetime(2020,3,1,tzinfo=timezone.utc)
APRIL=datetime(2020,4,1,tzinfo=timezone.utc)
JUNE=datetime(2020,6,1,tzinfo=timezone.utc)


def evidence_rows(*,source_issuer='I',filing_issuer='I',canonical_issuer=None):
    rows={domain:{} for domain in MODELS}
    filing=Filing(id='F',issuer_id=filing_issuer,source='sec',accession='A',form='10-Q',published_at=MARCH,
        available_at=MARCH,captured_at=JUNE,evidence={'original_publication_verified':True})
    document=FinancialSourceDocument(id='D',filing_id='F',content_hash='a'*64,source_url='https://example.test/source',
        available_at=MARCH,captured_at=JUNE,extraction_complete=True,disclosure_profile='periodic_report',
        parser_version='financial_extraction_v5',inventory={'counts':{'numeric_nodes':1,'extracted_nodes':1},'contexts':{'C':{}},'issues':[]})
    source=SourceFinancialFact(id='S',issuer_id=source_issuer,document_id='D',context_id='C',available_at=MARCH,
        captured_at=JUNE,extracted_at=JUNE,first_seen_at=JUNE,evidence={'source_node_ids':['N']})
    decision=SelectionDecision(id='A',record_domain='source_financial_fact',record_id='S',status='accepted',recorded_at=JUNE)
    rows['filing']['F']=filing;rows['financial_source_document']['D']=document;rows['source_financial_fact']['S']=source
    rows['selection_decision']['A']=decision
    if canonical_issuer:
        canonical=FundamentalFact(id='C',issuer_id=canonical_issuer,source_fact_id='S',available_at=MARCH,captured_at=JUNE,materialized_at=JUNE)
        rows['fundamental_fact']['C']=canonical
    return rows,source


def test_reconstructed_public_allows_later_frozen_adoption_but_observed_rejects_it():
    from backtest_data.releases.query import QueryRows,adopted,lineage_visible
    plain,source=evidence_rows()
    rows=QueryRows(plain)
    public=QueryContext('R',APRIL,'reconstructed_public','fundamentals_sql','periodic_report',True)
    observed=QueryContext('R',APRIL,'observed_system','fundamentals_sql','periodic_report',True)
    assert adopted(source,'source_financial_fact',rows,public)[0]
    assert lineage_visible(source,'source_financial_fact',rows,public)
    assert not adopted(source,'source_financial_fact',rows,observed)[0]
    assert not lineage_visible(source,'source_financial_fact',rows,observed)
    assert lineage_visible(rows['selection_decision']['A'],'selection_decision',rows,public)


def test_source_and_canonical_issuer_links_are_hard_integrity_requirements():
    from backtest_data.releases.validation import validate_rows
    rows,_=evidence_rows(source_issuer='OTHER')
    assert any(issue['reason']=='source_issuer_mismatch' for issue in validate_rows(rows,Path('.')))
    rows,_=evidence_rows(canonical_issuer='OTHER')
    assert any(issue['reason']=='canonical_issuer_mismatch' for issue in validate_rows(rows,Path('.')))


def test_successful_metrics_require_registered_definition_and_nonempty_lineage():
    from backtest_data.releases.query_context import definition_registry
    from backtest_data.releases.validation import validate_rows
    rows,_=evidence_rows()
    definition=definition_registry()['roe_parent_ttm']
    derived=DerivedFact(id='X',entity_id='I',entity_type='issuer',metric_id='roe_parent_ttm',definition_version=definition['definition_version'],
        definition_hash='b'*64,value=Decimal('.2'),status='ok',basis={'numeric_policy':definition['numeric_policy'],
        'disclosure_profile':'periodic_report'},input_refs=[],available_at=MARCH,captured_at=JUNE,
        materialized_at=JUNE,evaluation_as_of=JUNE,period={'period_end':'2019-12-31'})
    rows['derived_fact']['X']=derived
    reasons={issue['reason'] for issue in validate_rows(rows,Path('.'))}
    assert {'unsupported_metric_definition','successful_derived_lineage_missing'}.issubset(reasons)
    derived.definition_hash=definition['definition_hash'];derived.input_refs=[{'domain':'source_financial_fact','id':'S'}]
    assert not validate_rows(rows,Path('.'))


@pytest.mark.parametrize('status',['accepted','unavailable'])
def test_nonnull_metrics_cannot_bypass_lineage_using_another_status(status):
    from backtest_data.releases.query import QueryRows,lineage_visible
    from backtest_data.releases.query_context import definition_registry
    from backtest_data.releases.validation import validate_rows
    rows,_=evidence_rows()
    definition=definition_registry()['roe_parent_ttm']
    derived=DerivedFact(id='X',entity_id='I',entity_type='issuer',metric_id='roe_parent_ttm',
        definition_version=definition['definition_version'],definition_hash=definition['definition_hash'],
        value=Decimal('.2'),status=status,basis={'numeric_policy':definition['numeric_policy'],
        'disclosure_profile':'periodic_report'},input_refs=[],available_at=MARCH,captured_at=JUNE,
        materialized_at=JUNE,evaluation_as_of=JUNE,period={'period_end':'2019-12-31'})
    rows['derived_fact']['X']=derived
    rows['selection_decision']['AX']=SelectionDecision(id='AX',record_domain='derived_fact',record_id='X',status='accepted',recorded_at=JUNE)
    reasons={issue['reason'] for issue in validate_rows(rows,Path('.'))}
    assert {'metric_status_value_invalid','successful_derived_lineage_missing'}.issubset(reasons)
    public=QueryContext('R',APRIL,'reconstructed_public','fundamentals_sql','periodic_report',True)
    assert not lineage_visible(derived,'derived_fact',QueryRows(rows),public)
    derived.input_refs=[{'domain':'source_financial_fact','id':'S'}]
    assert any(issue['reason']=='metric_status_value_invalid' for issue in validate_rows(rows,Path('.')))
    assert not lineage_visible(derived,'derived_fact',QueryRows(rows),public)


def test_decision_index_is_built_once_for_many_fact_lookups():
    from backtest_data.releases.query import QueryRows,adopted
    class Instrumented(dict):
        calls=0
        def values(self):self.calls+=1;return super().values()
    decisions=Instrumented({str(i):SelectionDecision(id=str(i),record_domain='source_financial_fact',record_id=str(i),
        recorded_at=JUNE,status='accepted') for i in range(1000)})
    rows=QueryRows({'selection_decision':decisions})
    context=QueryContext('R',APRIL,'reconstructed_public','fundamentals_sql','periodic_report',True)
    for identity in decisions:
        source=SourceFinancialFact(id=identity)
        assert adopted(source,'source_financial_fact',rows,context)[0]
    assert decisions.calls==1


def test_incomplete_documents_and_future_period_or_basis_cannot_feed_metrics():
    from backtest_data.releases.query import QueryRows,lineage_visible
    rows,source=evidence_rows()
    public=QueryContext('R',APRIL,'reconstructed_public','fundamentals_sql','periodic_report',True)
    assert lineage_visible(source,'source_financial_fact',QueryRows(rows),public)
    rows['financial_source_document']['D'].extraction_complete=False
    assert not lineage_visible(source,'source_financial_fact',QueryRows(rows),public)
    rows['financial_source_document']['D'].extraction_complete=True
    source.period_end=JUNE.date()
    assert not lineage_visible(source,'source_financial_fact',QueryRows(rows),public)


def test_directory_target_declares_raw_archive_dependencies():
    from backtest_data.ingestion.models import TargetUniverseManifest
    from backtest_data.releases.build import dependencies
    target=TargetUniverseManifest(id='T',scope={'directory_evidence':[{'raw_id':'R','content_hash':'a'*64}]})
    assert dependencies(target)==[('raw_object','R')]


@pytest.mark.parametrize('mode',['reconstructed_public','observed_system'])
def test_direct_price_query_rejects_future_business_session(monkeypatch,mode):
    from backtest_data.catalog.models import SourceContract
    from backtest_data.ingestion.models import RawObject
    from backtest_data.prices.models import PriceObservation
    from backtest_data.releases import query
    from backtest_data.releases.models import DatasetRelease
    rows={domain:{} for domain in MODELS}
    price=PriceObservation(id='P',security_id='SEC',source_contract_id='SC',raw_id='RAW',session=JUNE.date(),
        basis='as_traded',feed='fixture',venue='US',trade_scope='regular',currency='USD',quantity_unit='shares',
        close=Decimal('60'),available_at=MARCH,captured_at=MARCH,evidence={})
    rows['price_observation']['P']=price
    rows['raw_object']['RAW']=RawObject(id='RAW')
    rows['source_contract']['SC']=SourceContract(id='SC')
    rows['selection_decision']['A']=SelectionDecision(id='A',record_domain='price_observation',record_id='P',status='accepted',recorded_at=MARCH)
    release=DatasetRelease(id='R',content_hash='a'*64,manifest={'tier':'research_partial','policies':{},'coverage':{},
        'target_count':1,'target_security_ids':['SEC'],'catalog':{'securities':{'SEC':{'name':'Security'}}}})
    monkeypatch.setattr(query,'database_session',lambda database:nullcontext(None))
    monkeypatch.setattr(query,'query_state',lambda session,context:(release,query.QueryRows(rows)))
    context=QueryContext('R',APRIL,mode,'core_price_volume','not_applicable',True)
    assert query.get_prices(None,['SEC'],context)['rows']==[]
    price.session=APRIL.date()
    utc_cutoff=APRIL-timedelta(minutes=30)
    utc=QueryContext('R',utc_cutoff,mode,'core_price_volume','not_applicable',True)
    seoul=QueryContext('R',utc_cutoff.astimezone(timezone(timedelta(hours=9))),mode,'core_price_volume','not_applicable',True)
    utc_response=query.get_prices(None,['SEC'],utc)
    assert utc_response['rows']==[]
    assert query.get_prices(None,['SEC'],seoul)==utc_response


def test_query_envelope_detaches_every_nested_manifest_payload():
    from backtest_data.releases.build import digest
    from backtest_data.releases.models import DatasetRelease
    from backtest_data.releases.query import envelope
    policies={'definitions':['version1']}
    coverage={'gaps':[{'reason':'original'}]}
    alias={'fields':{'urls':['https://example.test/original']}}
    manifest={'tier':'research_partial','target_count':1,'policies':policies,'coverage':coverage}
    release=DatasetRelease(id='R',content_hash=digest(manifest),manifest=manifest)
    context=QueryContext('R',APRIL,'reconstructed_public','fundamentals_sql','periodic_report',True)
    response=envelope(release,context,[{'provenance_aliases':[alias]}],extra={'audit':{'items':coverage['gaps']}})
    response['policies']['definitions'].append('mutated')
    response['coverage']['gaps'][0]['reason']='mutated'
    response['rows'][0]['provenance_aliases'][0]['fields']['urls'].append('mutated')
    response['audit']['items'].clear()
    assert policies=={'definitions':['version1']}
    assert coverage=={'gaps':[{'reason':'original'}]}
    assert alias['fields']['urls']==['https://example.test/original']
    assert digest(release.manifest)==release.content_hash


def test_price_business_session_cannot_be_captured_before_it_exists():
    from backtest_data.prices.models import PriceObservation
    from backtest_data.releases.validation import validate_rows
    rows={domain:{} for domain in MODELS}
    rows['price_observation']['P']=PriceObservation(id='P',session=JUNE.date(),available_at=MARCH,captured_at=MARCH,
        basis='as_traded',raw_id='RAW',source_contract_id='SC',evidence={})
    assert any(issue['reason']=='price_session_after_capture' for issue in validate_rows(rows,Path('.')))
