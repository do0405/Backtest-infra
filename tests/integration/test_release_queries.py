from argparse import Namespace
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from backtest_data.catalog.models import CatalogObservation,Issuer, Security, SourceContract
from backtest_data.ingestion.models import RawObject, TargetUniverseManifest
from backtest_data.ingestion.raw_store import RawStore
from backtest_data.prices.models import PriceObservation
from backtest_data.fundamentals.models import Filing, FinancialSourceDocument, SourceFinancialFact, DerivedFact
from backtest_data.releases.models import DatasetRelease, ReleasePointer, ReleaseSegment, SelectionDecision, SegmentMember, ValidationIssue

pytestmark=pytest.mark.integration


def stamp(month,day=1):
    return datetime(2020,month,day,tzinfo=timezone.utc)


def financial(session,issuer,value='100',*,month=3,first_seen=5):
    filing=Filing(issuer_id=issuer.id,source='sec',accession=str(uuid4()),form='10-Q',published_at=stamp(month),
                  available_at=stamp(month),captured_at=stamp(month),evidence={'original_publication_verified':True})
    session.add(filing);session.flush()
    doc=FinancialSourceDocument(filing_id=filing.id,content_hash=uuid4().hex*2,source_url='https://example.test/report',
            available_at=stamp(month),captured_at=stamp(month),extraction_complete=True,parser_version='financial_extraction_v4',
            accounting_basis='US_GAAP',assurance='audited',inventory={'counts':{'numeric_nodes':1,'extracted_nodes':1,
            'deduplicated_nodes':0,'unsupported_nodes':0},'contexts':{'C':{'id':'C'}},'units':{'U':'iso4217:USD'},'issues':[]})
    session.add(doc);session.flush()
    item=SourceFinancialFact(document_id=doc.id,issuer_id=issuer.id,concept='custom:Useful',account_name='Useful',context_id='C',
        dimensions={'{urn:custom}Region':'{urn:custom}North'},period_kind='instant',period_end=date(2019,12,31),scope='consolidated',
        unit='USD',original_unit='iso4217:USD',value=Decimal(value),raw_value=value,is_nil=False,
        available_at=stamp(month),captured_at=stamp(month),extracted_at=stamp(month,first_seen),first_seen_at=stamp(month,first_seen),
        signature=uuid4().hex*2,evidence={'source_node_ids':['N1'],'taxonomy':'urn:custom','parse_status':'ok','precision':'INF'})
    session.add(item);session.flush()
    session.add(SelectionDecision(record_domain='source_financial_fact',record_id=item.id,status='accepted',
        rule_version='source_financial_structure_v1',evidence_grade='contract_verified',reason='Extraction verified',actor='test',recorded_at=stamp(month,first_seen+1)))
    session.flush()
    return item,doc,filing


@pytest.fixture
def seeded(db_session):
    session=db_session
    issuer=Issuer(market='US',source_identifier=str(uuid4()),name='Original issuer')
    session.add(issuer);session.flush()
    sec=Security(issuer_id=issuer.id,market='US',source_identifier=str(uuid4()),security_type='common_stock',name='Original security')
    missing=Security(market='US',source_identifier=str(uuid4()),security_type='unclassified',name='Unresolved target')
    session.add_all([sec,missing]);session.flush()
    manifest=TargetUniverseManifest(content_hash=uuid4().hex*2,
        scope={'markets':['US'],'output_start':'2020-01-01','output_end':'2020-12-31','historical_universe_complete':False},
        members=[{'security_id':sec.id,'symbol':'EX','market':'US'},{'security_id':missing.id,'symbol':'MISSING','market':'US'}],
        gaps=[{'domain':'historical_universe','reason':'unqualified_lifecycle'}])
    session.add(manifest)
    root=Path('artifacts/tests/releases')/str(uuid4())
    raw=RawStore(root).put(b'{"price":"60"}')
    raw_row=RawObject(content_hash=raw.content_hash,storage_key=raw.storage_key,byte_count=raw.byte_count)
    contract=SourceContract(source='fixture',version=str(uuid4()),domain='prices',market='US',specification={'basis':'as_traded'},
                            qualification='contract_defined',content_hash=uuid4().hex*2)
    session.add_all([raw_row,contract]);session.flush()
    price=PriceObservation(security_id=sec.id,source_contract_id=contract.id,raw_id=raw_row.id,session=date(2020,2,28),
            basis='as_traded',feed='fixture',venue='US',trade_scope='regular',currency='USD',quantity_unit='shares',
            open=Decimal('60'),high=Decimal('60'),low=Decimal('60'),close=Decimal('60'),volume=Decimal('10'),notional=None,
            signature=uuid4().hex*2,available_at=stamp(3),captured_at=stamp(3),evidence={'availability_precision':'capture_only'})
    session.add(price);session.flush()
    decision=SelectionDecision(record_domain='price_observation',record_id=price.id,status='accepted',rule_version='raw_price_structure_v1',
            evidence_grade='contract_verified',reason='Verified structure',actor='test',recorded_at=stamp(3,3))
    session.add(decision)
    account,doc,filing=financial(session,issuer)
    session.flush()
    return {'session':session,'issuer':issuer,'security':sec,'missing':missing,'target':manifest,'raw_root':root,
            'price':price,'decision':decision,'account':account,'doc':doc,'filing':filing,'raw':raw_row,'contract':contract}


def build(fixture):
    from backtest_data.releases.build import build_release
    return build_release(fixture['session'],fixture['target'].id,raw_root=fixture['raw_root'])


def context(release_id,*,cutoff=stamp(7),mode='reconstructed_public',profile='fundamentals_sql',research=True):
    from backtest_data.releases.query_context import QueryContext
    return QueryContext(release_id,cutoff,mode,profile,'periodic_report' if profile=='fundamentals_sql' else 'not_applicable',research)


def alias(fixture, value='EX', *, security=None, when=stamp(3), status='accepted'):
    from backtest_data.catalog.aliases import register_alias
    return register_alias(fixture['session'], security_id=(security or fixture['security']).id,
        identifier=value, identifier_type='ticker', namespace='ticker', venue='US',
        valid_from=date(2020,1,1), available_at=when, captured_at=when, recorded_at=when,
        source='fixture', provenance={'source_document':'explicit_fixture'}, status=status)


def test_security_aliases_use_frozen_membership_and_catalog_only(seeded):
    from backtest_data.releases.query import get_security_aliases
    alias(seeded)
    first = build(seeded)['release_id']
    ctx = context(first, profile='security_identity', mode='observed_system')
    original = get_security_aliases(seeded['session'], ['ex'], ctx, market='US')
    assert original['rows'][0]['status'] == 'resolved'
    assert original['rows'][0]['security_id'] == seeded['security'].id
    assert original['rows'][0]['candidates'][0]['name'] == 'Original security'
    assert original['rows'][0]['candidates'][0]['issuer_id'] == seeded['issuer'].id
    seeded['security'].name = 'New label'
    seeded['security'].issuer_id = None
    alias(seeded, 'EX', security=seeded['missing'], when=stamp(6))
    alias(seeded, 'LATE', when=stamp(6))
    seeded['session'].flush()
    assert get_security_aliases(seeded['session'], ['ex'], ctx, market='US') == original
    assert get_security_aliases(seeded['session'], ['LATE'], ctx)['rows'][0]['status'] == 'unresolved'
    second = build(seeded)['release_id']
    later = get_security_aliases(seeded['session'], ['EX'], context(second, profile='security_identity'))
    assert later['rows'][0]['status'] == 'ambiguous'
    early = get_security_aliases(seeded['session'], ['EX'],
        context(second, cutoff=stamp(2), profile='security_identity'))
    assert early['rows'][0]['status'] == 'unresolved'


def test_alias_queries_fail_closed_for_ambiguous_unresolved_or_unlinked_inputs(seeded):
    from backtest_data.releases.query import cli_query
    alias(seeded)
    first = build(seeded)['release_id']
    options = dict(release_id=first, as_of=stamp(7).isoformat(), history_mode='observed_system',
        entity_id=[], identifier=['EX'], market='US', venue=None, namespace=None,
        session_date=None, research_partial=True)
    result = cli_query(seeded['session'], Namespace(action='prices', **options))
    assert result['status'] == 'research_partial'
    assert len(result['data']['rows']) == 1
    assert result['data']['identifier_resolutions'][0]['security_id'] == seeded['security'].id
    financial_result = cli_query(seeded['session'], Namespace(action='source-accounts', **options))
    assert len(financial_result['data']['rows']) == 1
    assert financial_result['data']['identifier_resolutions'][0]['candidates'][0]['issuer_id'] == seeded['issuer'].id
    alias(seeded, 'EX', security=seeded['missing'], when=stamp(6))
    alias(seeded, 'MISSING', security=seeded['missing'])
    second = build(seeded)['release_id']
    blocked = cli_query(seeded['session'], Namespace(action='prices', **{**options,'release_id':second}))
    assert blocked['status'] == 'blocked' and blocked['reason'] == 'identifier_ambiguous'
    missing = cli_query(seeded['session'], Namespace(action='prices', **{**options,'identifier':['UNKNOWN']}))
    assert missing['status'] == 'blocked' and missing['reason'] == 'identifier_unresolved'
    unlinked = cli_query(seeded['session'], Namespace(action='source-accounts',
        **{**options, 'release_id':second, 'identifier':['MISSING']}))
    assert unlinked['status'] == 'blocked' and unlinked['reason'] == 'issuer_link_unavailable'


def test_alias_resolution_policy_is_bound_to_release(seeded):
    from backtest_data.catalog.aliases import alias_policy
    alias(seeded)
    release_id = build(seeded)['release_id']
    manifest = seeded['session'].get(DatasetRelease, release_id).manifest
    assert manifest['policies']['security_alias_policy'] == alias_policy()
    assert manifest['policies']['query_version'] == 'release_query_v5'
    assert manifest['coverage']['profiles']['security_identity'] is False
    assert manifest['coverage']['security_aliases']['security_ids_with_assertions'] == 1


def test_alias_cli_rejects_mixed_ids_and_metric_entity_guessing(seeded):
    from backtest_data.releases.query import cli_query
    alias(seeded)
    release_id = build(seeded)['release_id']
    options = dict(release_id=release_id, as_of=stamp(7).isoformat(), history_mode='observed_system',
        entity_id=[], identifier=['EX'], research_partial=True)
    with pytest.raises(ValueError, match='mixed_entity'):
        cli_query(seeded['session'], Namespace(action='prices',
            **{**options,'entity_id':[seeded['security'].id]}))
    with pytest.raises(ValueError, match='explicit_issuer_or_security'):
        cli_query(seeded['session'], Namespace(action='metrics', **options))


def test_alias_reassignment_outside_target_suppresses_old_mapping_and_seals_correction(seeded):
    from backtest_data.catalog.aliases import register_alias
    from backtest_data.releases.query import get_security_aliases
    original = alias(seeded)
    first = build(seeded)['release_id']
    outside_issuer = Issuer(id=str(uuid4()), market='US', source_identifier=str(uuid4()), name='Outside issuer')
    seeded['session'].add(outside_issuer)
    seeded['session'].flush()
    outside = Security(id=str(uuid4()), issuer_id=outside_issuer.id, market='US', source_identifier=str(uuid4()),
        security_type='depositary_receipt', name='Outside target correction')
    seeded['session'].add(outside)
    seeded['session'].flush()
    changed = register_alias(seeded['session'], security_id=outside.id, identifier='EX',
        identifier_type='ticker', namespace='ticker', venue='US', valid_from=date(2020,1,1),
        available_at=stamp(6), captured_at=stamp(6), recorded_at=stamp(6), source='fixture',
        provenance={'source_document':'corrected'}, assertion_key=original.evidence['assertion_key'])
    second = build(seeded)['release_id']
    frozen = seeded['session'].get(DatasetRelease, second).manifest
    assert outside.id in frozen['catalog']['securities']
    assert outside.id not in frozen['target_security_ids']
    assert outside_issuer.id in frozen['catalog']['issuers']
    assert outside_issuer.id not in frozen['target_issuer_ids']
    output = get_security_aliases(seeded['session'], ['EX'], context(second, profile='security_identity'))
    assert output['rows'][0]['status'] == 'unresolved'
    assert get_security_aliases(seeded['session'], ['EX'],
        context(first, profile='security_identity'))['rows'][0]['security_id'] == seeded['security'].id
    assert get_security_aliases(seeded['session'], ['EX'],
        context(second, cutoff=stamp(5), profile='security_identity'))['rows'][0]['security_id'] == seeded['security'].id


def test_canonical_query_preserves_frozen_mapping_and_materialization_clock(seeded):
    from backtest_data.fundamentals.models import FundamentalFact
    from backtest_data.releases.query import get_fundamentals
    before = build(seeded)['release_id']
    source = seeded['account']
    canonical = FundamentalFact(issuer_id=source.issuer_id, source_fact_id=source.id,
        metric_id='assets_total', mapping_version='fundamental_mapping_v1', value=source.value,
        unit=source.unit, period={'kind':'instant','end':source.period_end.isoformat()},
        basis={'scope':'unknown','accounting_basis':'unknown','fiscal_lineage':None},
        available_at=source.available_at, captured_at=source.captured_at, materialized_at=stamp(6))
    seeded['session'].add(canonical)
    seeded['session'].flush()
    seeded['session'].add(SelectionDecision(record_domain='fundamental_fact',record_id=canonical.id,status='accepted',
        rule_version='fundamental_mapping_v1',evidence_grade='research_only',reason='Explicit mapping',actor='test',recorded_at=stamp(6)))
    seeded['session'].flush()
    after = build(seeded)['release_id']
    assert not get_fundamentals(seeded['session'],[],context(before))['rows']
    assert not get_fundamentals(seeded['session'],[],context(after,cutoff=stamp(5),mode='observed_system'))['rows']
    public = get_fundamentals(seeded['session'],[],context(after,cutoff=stamp(5)))['rows']
    assert len(public)==1 and public[0]['metric_id']=='assets_total'
    assert public[0]['scope']=='unknown' and 'scope_unqualified' in public[0]['qualification_gaps']
    assert public[0]['source_fact']['id']==source.id
    assert public[0]['mapping_record_id']==canonical.id
    assert get_fundamentals(seeded['session'],[],context(after),metric_ids=['revenue'])['rows']==[]


def test_rejected_canonical_mapping_is_not_reported_as_mapped(seeded):
    from backtest_data.fundamentals.models import FundamentalFact
    from backtest_data.releases.query import get_source_accounts
    source=seeded['account']
    canonical=FundamentalFact(issuer_id=source.issuer_id,source_fact_id=source.id,metric_id='assets_total',
        mapping_version='fundamental_mapping_v1',value=source.value,unit=source.unit,period={},basis={},
        available_at=source.available_at,captured_at=source.captured_at,materialized_at=stamp(4))
    seeded['session'].add(canonical)
    seeded['session'].flush()
    seeded['session'].add(SelectionDecision(record_domain='fundamental_fact',record_id=canonical.id,status='rejected',
        rule_version='fundamental_mapping_v1',evidence_grade='research_only',reason='Rejected mapping',actor='test',recorded_at=stamp(5)))
    seeded['session'].flush()
    release=build(seeded)['release_id']
    rows=get_source_accounts(seeded['session'],[],context(release))['rows']
    assert rows[0]['mapping_status']=='unmapped' and rows[0]['mapping_record_ids']==[]


def test_seal_full_target_closure_shared_segments_and_immutable_membership(seeded):
    result=build(seeded)
    assert result['status']=='sealed' and result['tier']=='research_partial' and result['target_count']==2
    release=seeded['session'].get(DatasetRelease,result['release_id'])
    assert release.manifest['catalog']['securities'][seeded['missing'].id]['security_type']=='unclassified'
    assert seeded['session'].scalar(select(ReleasePointer)) is None
    member_rows=seeded['session'].execute(select(SegmentMember.record_domain,SegmentMember.record_id).join(ReleaseSegment,ReleaseSegment.segment_id==SegmentMember.segment_id).where(ReleaseSegment.release_id==release.id)).all()
    assert ('source_financial_fact',seeded['account'].id) in member_rows
    assert ('financial_source_document',seeded['doc'].id) in member_rows
    assert ('filing',seeded['filing'].id) in member_rows
    assert ('raw_object',seeded['raw'].id) in member_rows
    assert ('source_contract',seeded['contract'].id) in member_rows
    repeat=build(seeded)
    assert repeat['release_id']==release.id
    segment=seeded['session'].scalar(select(ReleaseSegment).where(ReleaseSegment.release_id==release.id))
    with pytest.raises(DBAPIError,match='immutable'):
        with seeded['session'].begin_nested():
            seeded['session'].add(SegmentMember(segment_id=segment.segment_id,record_domain='raw_object',record_id=str(uuid4())))
            seeded['session'].flush()


def test_frozen_membership_labels_and_decisions_survive_later_live_rows(seeded):
    from backtest_data.releases.query import get_prices, get_source_accounts
    first=build(seeded)['release_id']
    original=get_source_accounts(seeded['session'],[seeded['issuer'].id],context(first))
    initial_prices=get_prices(seeded['session'],[seeded['security'].id],context(first,profile='core_price_volume'))
    financial(seeded['session'],seeded['issuer'],'110',month=6)
    seeded['issuer'].name='Changed issuer';seeded['security'].name='Changed security';seeded['security'].issuer_id=None
    seeded['session'].add(SelectionDecision(record_domain='price_observation',record_id=seeded['price'].id,status='rejected',
        rule_version='raw_price_structure_v1',evidence_grade='contract_verified',reason='Later rejection',actor='test',recorded_at=stamp(6)))
    seeded['session'].flush()
    assert get_source_accounts(seeded['session'],[seeded['issuer'].id],context(first))==original
    assert get_prices(seeded['session'],[seeded['security'].id],context(first,profile='core_price_volume'))==initial_prices
    assert original['rows'][0]['issuer_name']=='Original issuer'


def test_public_and_observed_clocks_future_decisions_and_strict_quality(seeded):
    from backtest_data.releases.query import get_prices,get_source_accounts
    release_id=build(seeded)['release_id']
    assert get_source_accounts(seeded['session'],[seeded['issuer'].id],context(release_id,cutoff=stamp(3,7)))['rows']
    assert not get_source_accounts(seeded['session'],[seeded['issuer'].id],context(release_id,cutoff=stamp(3,4),mode='observed_system'))['rows']
    assert not get_prices(seeded['session'],[seeded['security'].id],context(release_id,cutoff=stamp(3,2),profile='core_price_volume',mode='observed_system'))['rows']
    with pytest.raises(ValueError,match='partial'):
        get_prices(seeded['session'],[seeded['security'].id],context(release_id,profile='core_price_volume',research=False))


def test_new_release_keeps_original_and_revision_with_asof_gates(seeded):
    from backtest_data.releases.query import get_source_accounts
    first=build(seeded)['release_id']
    newer,_,_=financial(seeded['session'],seeded['issuer'],'110',month=6)
    second=build(seeded)['release_id']
    assert first!=second
    rows=get_source_accounts(seeded['session'],[seeded['issuer'].id],context(second))['rows']
    assert {row['value'] for row in rows}=={'100','110'}
    before=get_source_accounts(seeded['session'],[seeded['issuer'].id],context(second,cutoff=stamp(5)))['rows']
    assert [row['value'] for row in before]==['100'] and newer.id not in [row['id'] for row in before]
    first_segments={row.segment_id for row in seeded['session'].scalars(select(ReleaseSegment).where(ReleaseSegment.release_id==first))}
    second_segments={row.segment_id for row in seeded['session'].scalars(select(ReleaseSegment).where(ReleaseSegment.release_id==second))}
    assert first_segments&second_segments


def test_missing_derived_parent_and_unknown_disclosure_block_atomic_build(seeded):
    bad=DerivedFact(entity_id=seeded['issuer'].id,entity_type='issuer',metric_id='roe_parent_ttm',definition_version='financial_metrics_v1',
        definition_hash=uuid4().hex*2,signature=uuid4().hex*2,value=None,unit='ratio',status='unavailable',reason='missing',
        period={},basis={'disclosure_profile':'periodic_report'},input_refs=[{'domain':'source_financial_fact','id':str(uuid4())}],
        available_at=stamp(3),captured_at=stamp(3),materialized_at=stamp(3),evaluation_as_of=stamp(3))
    seeded['session'].add(bad);seeded['session'].flush()
    with pytest.raises(ValueError,match='dependency'):
        build(seeded)
    assert seeded['session'].scalar(select(DatasetRelease)) is None


def test_raw_checksums_validation_and_metrics_missing_are_explicit(seeded):
    from backtest_data.releases.validation import validate_dataset
    from backtest_data.releases.query import get_financial_metrics
    release_id=build(seeded)['release_id']
    response=get_financial_metrics(seeded['session'],[seeded['issuer'].id],context(release_id),['roe_parent_ttm'])
    assert response['rows'][0]['value'] is None and response['rows'][0]['reason']=='canonical_or_derived_data_unavailable'
    path=seeded['raw_root']/seeded['raw'].storage_key
    path.write_bytes(b'corrupt')
    report=validate_dataset(seeded['session'],seeded['raw_root'])
    assert report['status']=='blocked' and any(issue['reason']=='raw_checksum_mismatch' for issue in report['issues'])


def test_frozen_segment_row_hash_detects_unauthorized_row_changes(seeded):
    from backtest_data.releases.query import get_source_accounts
    release_id=build(seeded)['release_id']
    with seeded['session'].begin_nested():
        seeded['session'].execute(text('ALTER TABLE source_financial_fact DISABLE TRIGGER immutable_evidence'))
        seeded['session'].execute(text('UPDATE source_financial_fact SET raw_value=:value WHERE id=:id'),{'value':'tampered','id':seeded['account'].id})
        seeded['session'].execute(text('ALTER TABLE source_financial_fact ENABLE TRIGGER immutable_evidence'))
        with pytest.raises(ValueError,match='hash'):
            get_source_accounts(seeded['session'],[seeded['issuer'].id],context(release_id))


def test_mixed_disclosure_profiles_and_undefined_decision_rules_are_hard_failures(seeded):
    _,doc,_=financial(seeded['session'],seeded['issuer'],'110',month=6)
    with seeded['session'].begin_nested():
        seeded['session'].execute(text('ALTER TABLE financial_source_document DISABLE TRIGGER immutable_evidence'))
        seeded['session'].execute(text("UPDATE financial_source_document SET disclosure_profile='earliest_ingested_disclosure' WHERE id=:id"),{'id':doc.id})
        seeded['session'].execute(text('ALTER TABLE financial_source_document ENABLE TRIGGER immutable_evidence'))
        with pytest.raises(ValueError,match='disclosure'):
            build(seeded)


def test_sql_extraction_inventory_decisions_are_supported_with_real_adoption_clock(seeded):
    from backtest_data.releases.query import get_source_accounts,cli_query
    seeded['session'].add(SelectionDecision(record_domain='source_financial_fact',record_id=seeded['account'].id,status='accepted',
        rule_version='sql_extraction_inventory_v1',evidence_grade='structural_only',reason='SQL inventory adopted',actor='test',recorded_at=stamp(5)))
    seeded['session'].flush()
    release_id=build(seeded)['release_id']
    rows=get_source_accounts(seeded['session'],[seeded['issuer'].id],context(release_id,mode='observed_system'))['rows']
    assert rows[0]['decision']['evidence_grade']=='structural_only'
    args=Namespace(action='smoke',release_id=release_id,as_of=stamp(7).isoformat(),history_mode='observed_system',entity_id=[],research_partial=True)
    assert cli_query(seeded['session'],args)['visible_source_accounts']==1


def test_nested_derived_parent_visibility_and_cycle_closure_are_verified(seeded):
    item=DerivedFact(entity_id=seeded['issuer'].id,entity_type='issuer',metric_id='roe_parent_ttm',definition_version='financial_metrics_v1',
        definition_hash=uuid4().hex*2,signature=uuid4().hex*2,value=Decimal('.2'),unit='ratio',status='ok',reason=None,
        period={},basis={'disclosure_profile':'periodic_report','history_mode':'reconstructed_public'},input_refs=[],
        available_at=stamp(3,7),captured_at=stamp(3,7),materialized_at=stamp(3,7),evaluation_as_of=stamp(3,7))
    seeded['session'].add(item);seeded['session'].flush()
    with seeded['session'].begin_nested():
        seeded['session'].execute(text('ALTER TABLE derived_fact DISABLE TRIGGER immutable_evidence'))
        seeded['session'].execute(text('UPDATE derived_fact SET input_refs=CAST(:refs AS jsonb) WHERE id=:id'),
            {'refs':'[{"domain":"derived_fact","id":"'+item.id+'"}]','id':item.id})
        seeded['session'].execute(text('ALTER TABLE derived_fact ENABLE TRIGGER immutable_evidence'))
        with pytest.raises(ValueError,match='cycle'):
            build(seeded)


def test_financial_provenance_aliases_are_frozen_catalog_snapshots(seeded):
    from backtest_data.releases.query import get_source_accounts
    observation=CatalogObservation(source='financial_provenance',identifier=seeded['doc'].id,content_hash=uuid4().hex*2,
        fields={'document_id':seeded['doc'].id,'source_url':'https://example.test/alias'},captured_at=stamp(4))
    seeded['session'].add(observation);seeded['session'].flush()
    release_id=build(seeded)['release_id']
    payload=get_source_accounts(seeded['session'],[seeded['issuer'].id],context(release_id))
    assert payload['rows'][0]['provenance_aliases'][0]['fields']['source_url']=='https://example.test/alias'
    seeded['session'].add(CatalogObservation(source='financial_provenance',identifier=seeded['doc'].id,content_hash=uuid4().hex*2,
        fields={'document_id':seeded['doc'].id,'source_url':'https://example.test/changed'},captured_at=stamp(5)))
    seeded['session'].flush()
    assert get_source_accounts(seeded['session'],[seeded['issuer'].id],context(release_id))==payload


def test_later_metric_materialization_is_reusable_for_public_historical_inputs(seeded):
    from backtest_data.releases.query import get_financial_metrics
    from backtest_data.releases.query_context import definition_registry
    definition=definition_registry()['roe_parent_ttm']
    item=DerivedFact(entity_id=seeded['issuer'].id,entity_type='issuer',metric_id='roe_parent_ttm',
        definition_version=definition['definition_version'],definition_hash=definition['definition_hash'],signature=uuid4().hex*2,
        value=Decimal('.2'),unit='ratio',status='ok',reason=None,period={'period_end':'2019-12-31'},
        basis={'numeric_policy':definition['numeric_policy'],'disclosure_profile':'periodic_report'},
        input_refs=[{'domain':'source_financial_fact','id':seeded['account'].id},{'domain':'selection_decision',
                    'id':seeded['session'].scalar(select(SelectionDecision.id).where(SelectionDecision.record_id==seeded['account'].id))}],
        available_at=stamp(3,7),captured_at=stamp(6),materialized_at=stamp(6),evaluation_as_of=stamp(6))
    seeded['session'].add(item);seeded['session'].flush()
    seeded['session'].add(SelectionDecision(record_domain='derived_fact',record_id=item.id,status='accepted',rule_version='derived_metric_policy_v1',
        evidence_grade='contract_verified',reason='Metric verified',actor='test',recorded_at=stamp(6,2)))
    seeded['session'].flush()
    release_id=build(seeded)['release_id']
    assert get_financial_metrics(seeded['session'],[seeded['issuer'].id],context(release_id,cutoff=stamp(4)),['roe_parent_ttm'])['rows'][0]['value']=='0.2'
    assert get_financial_metrics(seeded['session'],[seeded['issuer'].id],context(release_id,cutoff=stamp(4),mode='observed_system'),['roe_parent_ttm'])['rows'][0]['value'] is None


def test_successful_empty_or_unregistered_metric_cannot_be_sealed(seeded):
    from backtest_data.releases.query_context import definition_registry
    definition=definition_registry()['roe_parent_ttm']
    item=DerivedFact(entity_id=seeded['issuer'].id,entity_type='issuer',metric_id='roe_parent_ttm',
        definition_version=definition['definition_version'],definition_hash=definition['definition_hash'],signature=uuid4().hex*2,
        value=Decimal('.2'),unit='ratio',status='ok',reason=None,period={},basis={'numeric_policy':definition['numeric_policy'],'disclosure_profile':'periodic_report'},
        input_refs=[],available_at=stamp(3),captured_at=stamp(6),materialized_at=stamp(6),evaluation_as_of=stamp(6))
    seeded['session'].add(item);seeded['session'].flush()
    with pytest.raises(ValueError,match='lineage'):
        build(seeded)


def test_rejected_row_issues_are_frozen_by_raw_or_full_target_security(seeded):
    from backtest_data.releases.validation import frozen_rows
    rejected=RawStore(seeded['raw_root']).put(b'{"rejected_price_row":"malformed"}')
    rejected_raw=RawObject(content_hash=rejected.content_hash,storage_key=rejected.storage_key,byte_count=rejected.byte_count)
    seeded['session'].add(rejected_raw);seeded['session'].flush()
    issues=[ValidationIssue(record_domain='price_observation',record_id=None,rule='raw_price_structure_v1',severity='error',
            evidence={'raw_id':seeded['raw'].id,'row_index':4}),
        ValidationIssue(record_domain='price_observation',record_id=None,rule='raw_price_structure_v1',severity='error',
            evidence={'security_id':seeded['missing'].id,'raw_id':rejected_raw.id,'row_index':5}),
        ValidationIssue(record_domain='price_observation',record_id=None,rule='raw_price_structure_v1',severity='error',
            evidence={'raw_id':str(uuid4()),'security_id':str(uuid4()),'row_index':6})]
    seeded['session'].add_all(issues);seeded['session'].flush()
    result=build(seeded)
    release=seeded['session'].get(DatasetRelease,result['release_id'])
    rows=frozen_rows(seeded['session'],release)
    assert set(rows['validation_issue'])=={issues[0].id,issues[1].id}
    assert rejected_raw.id in rows['raw_object']
    assert result['counts']['validation_issue']==2


def directory_target(seeded,*,content_hash=None):
    archived=RawStore(seeded['raw_root']).put(b'symbol|security_name\nEX|Original security\n')
    raw=RawObject(content_hash=archived.content_hash,storage_key=archived.storage_key,byte_count=archived.byte_count)
    seeded['session'].add(raw);seeded['session'].flush()
    target=TargetUniverseManifest(content_hash=uuid4().hex*2,members=seeded['target'].members,gaps=seeded['target'].gaps,
        scope={**seeded['target'].scope,'directory_evidence':[{'raw_id':raw.id,
            'hash':content_hash or raw.content_hash,'url':'https://example.test/directory','rows':1}]})
    seeded['session'].add(target);seeded['session'].flush()
    seeded['target']=target
    return raw


def test_directory_provenance_archives_are_full_target_dependencies(seeded):
    from backtest_data.releases.validation import frozen_rows
    raw=directory_target(seeded)
    result=build(seeded)
    release=seeded['session'].get(DatasetRelease,result['release_id'])
    rows=frozen_rows(seeded['session'],release)
    assert raw.id in rows['raw_object']
    assert release.manifest['target_manifest']['scope']['directory_evidence'][0]['hash']==raw.content_hash


def test_directory_provenance_hash_mismatch_cannot_seal(seeded):
    directory_target(seeded,content_hash='0'*64)
    with pytest.raises(ValueError,match='directory_evidence_hash_mismatch'):
        build(seeded)
    assert seeded['session'].scalar(select(DatasetRelease)) is None


def test_mutating_returned_payload_cannot_change_sealed_json_in_reused_session(seeded):
    from backtest_data.releases.query import get_source_accounts
    observation=CatalogObservation(source='financial_provenance',identifier=seeded['doc'].id,content_hash=uuid4().hex*2,
        fields={'document_id':seeded['doc'].id,'urls':['https://example.test/original']},captured_at=stamp(4))
    seeded['session'].add(observation);seeded['session'].flush()
    release_id=build(seeded)['release_id']
    response=get_source_accounts(seeded['session'],[seeded['issuer'].id],context(release_id))
    original=deepcopy(response)
    response['policies']['metric_definitions'].append('mutated')
    response['coverage']['profiles']['fundamentals_sql']=True
    response['coverage']['gaps'].clear()
    response['rows'][0]['provenance_aliases'][0]['fields']['urls'].append('mutated')
    response['rows'][0]['document']['inventory']['counts']['numeric_nodes']=0
    assert get_source_accounts(seeded['session'],[seeded['issuer'].id],context(release_id))==original


def test_price_with_session_after_capture_cannot_be_sealed(seeded):
    price=PriceObservation(security_id=seeded['security'].id,source_contract_id=seeded['contract'].id,raw_id=seeded['raw'].id,
        session=date(2020,5,1),basis='as_traded',feed='fixture',venue='US',trade_scope='regular',currency='USD',quantity_unit='shares',
        open=Decimal('60'),high=Decimal('60'),low=Decimal('60'),close=Decimal('60'),volume=Decimal('10'),notional=None,
        signature=uuid4().hex*2,available_at=stamp(3),captured_at=stamp(3),evidence={})
    seeded['session'].add(price);seeded['session'].flush()
    with pytest.raises(ValueError,match='price_session_after_capture'):
        build(seeded)
    assert seeded['session'].scalar(select(DatasetRelease)) is None
