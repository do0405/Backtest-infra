from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from backtest_data.catalog.models import SourceContract
from backtest_data.fundamentals.daily_publication import POLICY_VERSION, derive_daily_publication
from backtest_data.fundamentals.daily_publication_models import FinancialDailyPublicationEvidence
from backtest_data.fundamentals.models import DerivedFact, Filing, FinancialSourceDocument, FundamentalFact, SourceFinancialFact
from backtest_data.prices.models import PriceObservation
from backtest_data.releases.models import SelectionDecision
from backtest_data.releases.query import QueryRows, visible
from backtest_data.releases.query_context import QueryContext, policy_bundle

CAPTURE = datetime(2026, 10, 10, tzinfo=timezone.utc)
MONDAY = datetime(2024, 2, 5, 14, 30, tzinfo=timezone.utc)
VERSION = 'release_query_v8_daily'


def context(as_of=MONDAY, mode='reconstructed_public', profile='fundamentals_sql'):
    return QueryContext('R', as_of, mode, profile, 'periodic_report' if profile == 'fundamentals_sql' else 'not_applicable', True)


def inputs(day=date(2024, 2, 2), *, suffix='1', value='100', source='sec', returned=None, proof_fact=False):
    accession = '20240202000001' if source == 'opendart' else f'0000000001-24-{int(suffix):06}'
    filing = Filing(id='F'+suffix, issuer_id='I', source=source, accession=accession, form='10-K',
        published_at=None, available_at=CAPTURE, captured_at=CAPTURE, evidence={})
    document = FinancialSourceDocument(id='D'+suffix, filing_id=filing.id, content_hash='a'*64,
        source_url='https://www.sec.gov/metadata', revision_kind='original', disclosure_profile='periodic_report',
        accounting_basis='US_GAAP', available_at=CAPTURE, captured_at=CAPTURE, extraction_complete=True,
        inventory={'counts':{'numeric_nodes':1,'extracted_nodes':1,'deduplicated_nodes':0}, 'contexts':{'C':{}}}, parser_version='fixture')
    evidence = {'source_node_ids':['N'], 'taxonomy':'http://fasb.org/us-gaap/2024', 'parse_status':'ok'}
    if returned: evidence['source_row'] = {'rcept_no':returned} if source == 'opendart' else {'accn':returned}
    fact = SourceFinancialFact(id='S'+suffix, document_id=document.id, issuer_id='I', concept='g:Assets',
        account_name='Assets', context_id='C', dimensions={}, period_kind='instant', period_start=None,
        period_end=date(2023,12,31), instant=date(2023,12,31), scope='consolidated', unit='USD', original_unit='USD',
        value=Decimal(value), raw_value=value, is_nil=False, scale=None, decimals='0', available_at=CAPTURE,
        captured_at=CAPTURE, first_seen_at=CAPTURE, extracted_at=CAPTURE, evidence=evidence)
    canonical = FundamentalFact(id='M'+suffix, issuer_id='I', source_fact_id=fact.id, metric_id='assets_total',
        mapping_version='fixture', value=Decimal(value), unit='USD', period={'kind':'instant','end':'2023-12-31'},
        basis={'scope':'consolidated','currency':'USD','accounting_basis':'US_GAAP'}, available_at=CAPTURE,
        captured_at=CAPTURE, materialized_at=CAPTURE)
    version = returned or accession
    metadata = {'rcept_no':version,'rcept_dt':day.isoformat()} if source == 'opendart' else {'accn':version,'filed':day.isoformat()}
    resolution = derive_daily_publication(document, filing, source_fact=fact if proof_fact else None,
        market='KR' if source == 'opendart' else 'US', metadata_records=[metadata])
    proof = FinancialDailyPublicationEvidence(id='E'+suffix, document_id=document.id, filing_id=filing.id,
        issuer_id='I', source=source, source_fact_id=fact.id if proof_fact else None,
        source_version_ref=version, status='qualified', public_day=day, effective_available_at=resolution['effective_available_at'],
        policy_version=POLICY_VERSION, evidence_grade='metadata_grade', captured_at=CAPTURE, recorded_at=CAPTURE,
        evidence=resolution)
    records = {'filing':{filing.id:filing}, 'financial_source_document':{document.id:document},
        'source_financial_fact':{fact.id:fact}, 'fundamental_fact':{canonical.id:canonical},
        'financial_daily_publication_evidence':{proof.id:proof}, 'selection_decision':{}, 'derived_fact':{}}
    for domain, row in [('source_financial_fact',fact),('fundamental_fact',canonical)]:
        decision = SelectionDecision(id='A'+row.id, record_domain=domain, record_id=row.id, status='accepted', recorded_at=CAPTURE)
        records['selection_decision'][decision.id] = decision
    rows = QueryRows(records, {'policies':policy_bundle(VERSION), 'catalog':{'issuers':{'I':{'name':'Issuer'}},'securities':{}}})
    return rows, fact, canonical


def merge(left, right):
    return QueryRows({key:{**left.get(key,{}), **right.get(key,{})} for key in left.keys() | right.keys()}, left.manifest)


def metric(rows, source, *, proof='E1', identity='T'):
    row = DerivedFact(id=identity, entity_id='I', metric_id='assets_total', value=source.value, status='ok', period={},
        available_at=MONDAY, captured_at=CAPTURE, materialized_at=CAPTURE, evaluation_as_of=MONDAY,
        basis={'public_availability_policy':POLICY_VERSION,'history_mode':'reconstructed_public'},
        input_refs=[{'domain':'source_financial_fact','id':source.id},
            {'domain':'financial_daily_publication_evidence','id':proof}])
    rows['derived_fact'][identity] = row
    return row


def query_fixture(monkeypatch, rows):
    from backtest_data.releases import query
    manifest = {**rows.manifest,'target_issuer_ids':['I'],'target_security_ids':[],'tier':'research_partial',
        'target_count':17758,'coverage':{}}
    release = SimpleNamespace(id='R',content_hash='h',manifest=manifest)
    monkeypatch.setattr(query,'database_session',lambda _:nullcontext(None))
    monkeypatch.setattr(query,'query_state',lambda *_:(release,rows))
    return query


def test_daily_financial_visibility_uses_metadata_without_strict_archive_certificate():
    from backtest_data.releases.public_clock import row_availability
    rows, source, canonical = inputs()
    for row in (source, canonical):
        assert not visible(row,context(MONDAY-timedelta(seconds=1)),rows)
        assert visible(row,context(),rows)
        result = row_availability(row,rows,context())
        assert result['evidence_grade'] == 'metadata_grade'
        assert result['metadata_public_bound'] == MONDAY
        assert result['public_day'] == date(2024,2,2)
        assert result['original_archive_verified'] is False
        assert row.available_at == CAPTURE
    assert not visible(source,context(mode='observed_system'),rows)
    assert visible(source,context(CAPTURE,mode='observed_system'),rows)


def test_daily_missing_frozen_metadata_never_falls_back_to_late_capture():
    rows, source, _ = inputs()
    rows['financial_daily_publication_evidence'] = {}
    assert not visible(source,context(CAPTURE),rows)


def test_exact_returned_receipt_qualifies_source_without_default_document_evidence(monkeypatch):
    from backtest_data.releases.public_clock import row_availability
    rows, source, canonical = inputs(day=date(2024,4,5), source='opendart', returned='20240405000001', proof_fact=True)
    cutoff = context(datetime(2024,4,8,tzinfo=timezone.utc))
    assert row_availability(source,rows,cutoff)['source_version_ref'] == '20240405000001'
    assert visible(canonical,cutoff,rows)
    query = query_fixture(monkeypatch,rows)
    assert len(query.get_source_accounts(None,['I'],cutoff)['rows']) == 1
    assert len(query.get_fundamentals(None,['I'],cutoff)['rows']) == 1


def test_daily_derived_pins_its_metadata_proofs():
    from backtest_data.releases.public_clock import row_availability
    rows, source, _ = inputs()
    derived = metric(rows,source)
    assert visible(derived,context(),rows)
    assert row_availability(derived,rows,context())['evidence_ids'] == ['E1']
    derived.input_refs[-1]['id'] = 'missing'
    assert not visible(derived,context(CAPTURE),rows)


def test_daily_wrong_policy_derived_does_not_use_its_physical_capture_clock():
    rows, source, _ = inputs()
    derived = metric(rows,source)
    derived.basis['public_availability_policy'] = 'financial_public_pit_v1'
    assert not visible(derived,context(CAPTURE),rows)


def test_latest_source_uses_public_day_when_weekend_revisions_share_monday(monkeypatch):
    first, _, _ = inputs()
    correction, _, _ = inputs(day=date(2024,2,3),suffix='2',value='80')
    rows = merge(first,correction)
    query = query_fixture(monkeypatch,rows)
    result = query.get_source_accounts(None,['I'],context(),latest_visible=True)
    assert [row['value'] for row in result['rows']] == ['80']
    assert result['public_version_order'] == 'metadata_public_day'
    assert result['target_count'] == 17758


def test_same_public_day_different_values_remain_ambiguous(monkeypatch):
    first, _, _ = inputs()
    correction, _, _ = inputs(suffix='2',value='80')
    rows = merge(first,correction)
    query = query_fixture(monkeypatch,rows)
    result = query.get_source_accounts(None,['I'],context(),latest_visible=True)
    assert result['rows'] == []
    assert result['ambiguous_revision_groups'] == 1


def test_daily_metric_rejects_superseded_weekend_source(monkeypatch):
    from backtest_data.releases import query
    first, source, _ = inputs()
    correction, _, _ = inputs(day=date(2024,2,3),suffix='2',value='80')
    rows = merge(first,correction)
    derived = metric(rows,source)
    assert not query.metric_public_revisions_current(derived,rows,context())


def test_v8_smoke_counts_metadata_visible_sources_consistently_with_query(monkeypatch):
    rows,_,_ = inputs()
    query = query_fixture(monkeypatch,rows)
    result = query.cli_query(None,SimpleNamespace(action='smoke',release_id='R',as_of=MONDAY.isoformat(),
        history_mode='reconstructed_public',entity_id=['I'],research_partial=True))
    assert result['visible_source_accounts'] == 1


@pytest.mark.parametrize('basis', ['as_traded','provider_adjusted'])
def test_cli_forwards_explicit_supplier_basis_with_raw_default(monkeypatch,basis):
    from backtest_data.releases import query
    received=[]
    def prices(database,ids,cutoff,**kwargs):
        received.append(kwargs.get('basis')); return {'rows':[]}
    monkeypatch.setattr(query,'get_prices',prices)
    args=SimpleNamespace(action='prices',release_id='R',as_of=MONDAY.isoformat(),
        history_mode='reconstructed_public',entity_id=[],research_partial=True)
    if basis=='provider_adjusted':args.basis=basis
    query.cli_query(None,args)
    assert received == [basis]


@pytest.mark.parametrize('domain', ['source_financial_fact','fundamental_fact'])
def test_daily_wrong_issuer_link_is_unavailable(domain):
    rows,source,canonical = inputs()
    (source if domain=='source_financial_fact' else canonical).issuer_id = 'OTHER'
    assert not visible(source if domain=='source_financial_fact' else canonical,context(CAPTURE),rows)


def test_daily_observed_source_keeps_first_seen_and_extraction_gates():
    rows,source,_ = inputs()
    source.first_seen_at = CAPTURE+timedelta(days=1)
    assert visible(source,context(),rows)
    assert not visible(source,context(CAPTURE,mode='observed_system'),rows)


def test_daily_cache_scans_each_document_once_but_resolves_exact_fact_identity(monkeypatch):
    from backtest_data.releases import public_clock
    rows,source,canonical = inputs()
    for index in range(1000):
        rows['financial_daily_publication_evidence'][str(index)] = SimpleNamespace(document_id=str(index))
    original = public_clock.resolve_daily_availability
    inspected=[]
    def counted(document,filing,evidence,**kwargs):
        proofs=list(evidence); inspected.append((kwargs.get('source_fact').id,len(proofs)))
        return original(document,filing,proofs,**kwargs)
    monkeypatch.setattr(public_clock,'resolve_daily_availability',counted)
    for _ in range(20):
        assert visible(source,context(),rows)
        assert visible(canonical,context(),rows)
    assert inspected == [('S1',1)]


def test_daily_nested_metric_cannot_regain_metadata_excluded_by_parent():
    rows,source,_ = inputs()
    inner=metric(rows,source)
    other,_,_ = inputs(suffix='2')
    rows=merge(rows,other)
    outer=DerivedFact(id='U',basis={'public_availability_policy':POLICY_VERSION},
        input_refs=[{'domain':'derived_fact','id':inner.id},{'domain':'financial_daily_publication_evidence','id':'E2'}])
    assert not visible(outer,context(CAPTURE),rows)


def price_inputs(market='US', basis='provider_adjusted', flag='split'):
    row = PriceObservation(id='P',security_id='Q',source_contract_id='C',session=date(2024,3,1),basis=basis,
        available_at=CAPTURE,captured_at=CAPTURE,evidence={})
    contract = SourceContract(id='C',source='alpaca',specification={'endpoint':'/v2/stocks/{symbol}/bars','adjustment':flag})
    rows = QueryRows({'price_observation':{'P':row},'source_contract':{'C':contract}},
        {'policies':policy_bundle(VERSION),'catalog':{'securities':{'Q':{'market':market}}}})
    return row,rows


@pytest.mark.parametrize(('market','bound'), [('US',datetime(2024,3,2,5,tzinfo=timezone.utc)),('KR',datetime(2024,3,1,15,tzinfo=timezone.utc))])
@pytest.mark.parametrize(('basis','flag'), [('as_traded','raw'),('provider_adjusted','split')])
def test_v8_declared_price_bases_use_local_completed_day(market,bound,basis,flag):
    from backtest_data.releases.market_clock import availability
    row,rows = price_inputs(market,basis,flag)
    assert not visible(row,context(bound-timedelta(seconds=1),profile='core_price_volume'),rows)
    assert visible(row,context(bound,profile='core_price_volume'),rows)
    assert not visible(row,context(bound,mode='observed_system',profile='core_price_volume'),rows)
    result = availability(row,rows)
    assert result['policy'] == 'completed_local_day_supplier_basis_v1'
    assert result['price_basis_policy']['basis'] == basis
    assert result['adjustment_action_coverage_verified'] is False
    assert row.available_at == CAPTURE


@pytest.mark.parametrize('change', ['basis','missing_contract','unknown_flag','contradictory_row_definition'])
def test_v8_price_contract_mismatch_fails_closed(change):
    row,rows = price_inputs()
    if change == 'basis': row.basis = 'as_traded'
    if change == 'missing_contract': rows['source_contract'] = {}
    if change == 'unknown_flag': rows['source_contract']['C'].specification['adjustment'] = 'unknown'
    if change == 'contradictory_row_definition': row.evidence['price_basis_policy'] = {'policy':'supplier_declared_price_basis_v1','basis':'provider_adjusted','adjustments':['dividend']}
    assert not visible(row,context(CAPTURE,profile='core_price_volume'),rows)


def test_v8_validation_projects_daily_inputs_but_retains_processing_checks(monkeypatch):
    from backtest_data.releases import validation
    from backtest_data.releases.build import MODELS
    rows,source,_ = inputs()
    derived = metric(rows,source)
    for domain in MODELS: rows.setdefault(domain,{})
    monkeypatch.setattr(validation,'registered_definition',lambda _:True)
    issues = validation.validate_rows(rows,Path('artifacts/tests/no-raw'),policies=policy_bundle(VERSION))
    assert not any(item['reason']=='derived_future_input' for item in issues)
    derived.materialized_at = MONDAY
    issues = validation.validate_rows(rows,Path('artifacts/tests/no-raw'),policies=policy_bundle(VERSION))
    assert any(item['reason']=='derived_processing_backdated' for item in issues)
