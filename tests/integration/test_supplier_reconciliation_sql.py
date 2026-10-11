from datetime import date,datetime,timedelta,timezone
from decimal import Decimal as D
from uuid import uuid4
import pytest
from sqlalchemy import select,func
from backtest_data.catalog.models import Security,SourceContract
from backtest_data.ingestion.models import RawObject
from backtest_data.prices.models import PriceObservation
from backtest_data.prices.validation import ComparisonContract
from backtest_data.releases.models import SelectionDecision,ValidationIssue

pytestmark=pytest.mark.integration
STAMP=datetime(2026,10,10,12,tzinfo=timezone.utc)


def stored(db,security,*,source='alpaca',adjustment='split',close='11',explicit_context=True,
    specification=None,stored_feed='sip',stored_venue='US_CONSOLIDATED',stored_session=date(2016,1,4)):
    identity=str(uuid4())
    spec={'endpoint':'/v2/stocks/{symbol}/bars','request_parameters':{'adjustment':adjustment},
        'price_unit':'currency_per_share','quantity_unit':'shares'}
    if source=='kis_overseas':spec.update(endpoint='/uapi/overseas-price/v1/quotations/dailyprice',request_parameters={'MODP':'1' if adjustment!='raw' else '0'})
    if explicit_context:spec.update(session_scope='new_york_calendar_day',adjustment_reference_basis='current:2026-10-10',adjustment_reference_evidence_ref='fixture-source-declaration')
    if specification is not None:spec=dict(specification)
    contract=SourceContract(source=source,version=identity,domain='prices',market='US',specification=spec,
        qualification='supplier_declared_research',content_hash=identity.replace('-','').ljust(64,'0'),created_at=STAMP)
    raw=RawObject(content_hash=identity.replace('-','').ljust(64,'1'),storage_key='fixture/'+identity,byte_count=4)
    db.add_all([contract,raw]);db.flush()
    row=PriceObservation(security_id=security.id,source_contract_id=contract.id,raw_id=raw.id,
        session=stored_session,basis='as_traded' if adjustment=='raw' else 'provider_adjusted',
        feed=stored_feed,venue=stored_venue,trade_scope=spec.get('trade_scope','daily_condition_filtered'),currency='USD',quantity_unit='shares',
        open=D('10'),high=D('12'),low=D('9'),close=D(close),volume=D('100'),notional=None,
        signature=identity.replace('-','').ljust(64,'2'),captured_at=STAMP,available_at=STAMP,
        evidence={'basis_reference_captured_at':STAMP.isoformat()})
    db.add(row);db.flush();return row


def security(db):
    row=Security(market='US',source_identifier='reconciliation-'+str(uuid4()),security_type='common_stock',name='fixture')
    db.add(row);db.flush();return row


def run(db,rows,*,as_of=STAMP,recorded_at=STAMP+timedelta(hours=1),**kwargs):
    from backtest_data.prices.reconciliation_sql import reconcile_stored_supplier_prices
    return reconcile_stored_supplier_prices(db,[row.id for row in rows],as_of=as_of,
        recorded_at=recorded_at,contract=ComparisonContract('fixture-tick',tick_size=D('.01')),**kwargs)


def test_unique_two_of_three_sql_nomination_replays_without_changing_source_values_or_clocks(db_session):
    sec=security(db_session)
    rows=[stored(db_session,sec),stored(db_session,sec,close='11.005'),stored(db_session,sec,close='11.1')]
    originals=[(row.id,row.close,row.captured_at,row.available_at) for row in rows]
    first=run(db_session,rows)
    assert first['status']=='accepted' and first['selected_observation_id'] in {row.id for row in rows[:2]}
    assert first['dissenting_observation_ids']==[rows[2].id]
    assert first['inserted_decisions']==first['inserted_issues']==1
    assert first['declaration_grade']=='supplier_declared' and first['corroboration_grade']=='agreement_independence_unknown'
    second=run(db_session,list(reversed(rows)),recorded_at=STAMP+timedelta(days=1))
    assert second['inserted_decisions']==second['inserted_issues']==0
    assert second['decision_id']==first['decision_id'] and second['recorded_at']==first['recorded_at']
    assert [(row.id,row.close,row.captured_at,row.available_at) for row in rows]==originals
    decision=db_session.get(SelectionDecision,first['decision_id'])
    assert decision.evidence_grade=='supplier_declared'
    issue=db_session.get(ValidationIssue,first['issue_id'])
    assert issue.evidence['dissenting_observation_ids']==[rows[2].id]
    assert issue.evidence['comparison_contract']['tick_size']=='.01' or issue.evidence['comparison_contract']['tick_size']=='0.01'


def test_sql_unspecified_kis_split_alpaca_and_capture_clock_never_certify_reference(db_session):
    sec=security(db_session)
    rows=[stored(db_session,sec,source='kis_overseas',explicit_context=False),stored(db_session,sec,explicit_context=False)]
    result=run(db_session,rows,candidate_id=rows[1].id)
    assert result['status']=='accepted' and result['selected_observation_id']==rows[1].id
    assert result['corroboration_grade']=='not_corroborated'
    assert result['comparisons'][0]['status']=='incomparable'
    assert 'unknown_adjustment_reference' in {issue['code'] for issue in result['comparisons'][0]['issues']}
    assert result['inserted_decisions']==1


def test_sql_missing_ids_and_future_cutoff_fail_before_audit_or_selection(db_session):
    from backtest_data.prices.reconciliation_sql import reconcile_stored_supplier_prices
    sec=security(db_session);row=stored(db_session,sec)
    before=(db_session.scalar(select(func.count()).select_from(ValidationIssue)),db_session.scalar(select(func.count()).select_from(SelectionDecision)))
    for ids,clock in (([row.id,str(uuid4())],STAMP),([row.id],STAMP+timedelta(days=2))):
        with pytest.raises(ValueError):
            reconcile_stored_supplier_prices(db_session,ids,as_of=clock,recorded_at=STAMP,
                contract=ComparisonContract('fixture-tick',tick_size=D('.01')))
    assert before==(db_session.scalar(select(func.count()).select_from(ValidationIssue)),db_session.scalar(select(func.count()).select_from(SelectionDecision)))


def test_sql_two_disagreeing_values_only_append_audit_and_preserve_prior_decisions(db_session):
    sec=security(db_session);rows=[stored(db_session,sec),stored(db_session,sec,close='11.1')]
    previous=SelectionDecision(record_domain='price_observation',record_id=rows[0].id,status='accepted',
        rule_version='provider_declared_price_structure_v1',evidence_grade='supplier_declared',reason='original declaration',actor='collector',recorded_at=STAMP)
    db_session.add(previous);db_session.flush()
    result=run(db_session,rows)
    assert result['status']=='quarantined' and result['selected_observation_id'] is None
    assert result['inserted_decisions']==0 and result['inserted_issues']==1
    assert db_session.get(SelectionDecision,previous.id).status=='accepted'


def test_sql_declared_raw_kis_and_alpaca_with_unknown_scope_remain_incomparable(db_session):
    sec=security(db_session)
    rows=[stored(db_session,sec,source='kis_overseas',adjustment='raw',explicit_context=False),
        stored(db_session,sec,adjustment='raw',explicit_context=False)]
    result=run(db_session,rows,candidate_id=rows[0].id)
    assert result['status']=='accepted' and result['selected_observation_id']==rows[0].id
    assert result['comparisons'][0]['status']=='incomparable'
    assert result['corroboration_grade']=='not_corroborated'


def test_selection_identity_conflict_rolls_back_new_audit_atomically(db_session):
    sec=security(db_session);row=stored(db_session,sec)
    proof=run(db_session,[row],persist=False)
    collision=SelectionDecision(id=proof['decision_id'],record_domain='price_observation',record_id=row.id,
        status='accepted',rule_version='different-existing-rule',evidence_grade='supplier_declared',
        reason='unrelated receipt',actor='fixture',recorded_at=STAMP)
    db_session.add(collision);db_session.flush()
    with pytest.raises(ValueError,match='identity conflict'):run(db_session,[row])
    assert db_session.get(ValidationIssue,proof['issue_id']) is None
    assert db_session.get(SelectionDecision,proof['decision_id']).rule_version=='different-existing-rule'


def test_legacy_alpaca_sip_feed_label_uses_exact_frozen_api_contract_without_source_rewrite(db_session):
    from backtest_data.prices.alpaca_storage import ALPACA_SPEC
    sec=security(db_session)
    rows=[stored(db_session,sec,adjustment='raw',specification=ALPACA_SPEC,stored_feed='alpaca:sip'),
        stored(db_session,sec,adjustment='raw',specification=ALPACA_SPEC,close='11.005')]
    original=[(row.id,row.feed,row.close,row.captured_at,row.available_at) for row in rows]
    result=run(db_session,rows)
    assert result['status']=='accepted' and result['comparisons'][0]['status']=='agree'
    assert result['corroboration_grade']=='agreement_independence_unknown'
    assert run(db_session,rows)['inserted_issues']==0
    assert [(row.id,row.feed,row.close,row.captured_at,row.available_at) for row in rows]==original
    issue=db_session.get(ValidationIssue,result['issue_id'])
    assert issue.evidence['feed_normalization']==[{'observation_id':rows[0].id,'stored_feed':'alpaca:sip',
        'comparison_feed':'sip','policy':'alpaca_sip_feed_label_alias_v1'}]


@pytest.mark.parametrize('feed',['alpaca:iex','unrecognized','vendor:sip'])
def test_unknown_alpaca_feed_labels_are_rejected_before_auditing(db_session,feed):
    from backtest_data.prices.alpaca_storage import ALPACA_SPEC
    sec=security(db_session);row=stored(db_session,sec,adjustment='raw',specification=ALPACA_SPEC,stored_feed=feed)
    before=db_session.scalar(select(func.count()).select_from(ValidationIssue))
    with pytest.raises(ValueError,match='feed'):run(db_session,[row])
    assert db_session.scalar(select(func.count()).select_from(ValidationIssue))==before


@pytest.mark.parametrize('venue',['NAS','NYS','AMS'])
def test_registered_kis_us_exchange_template_preserves_native_feed_and_audits_incomparability(db_session,venue):
    from backtest_data.prices.kis_overseas_storage import KIS_OVERSEAS_SPEC
    sec=security(db_session)
    rows=[stored(db_session,sec,source='kis_overseas',adjustment='raw',specification=KIS_OVERSEAS_SPEC,
        stored_feed='kis:us:'+venue,stored_venue=venue),stored(db_session,sec)]
    original=[(row.id,row.feed,row.venue,row.close,row.captured_at,row.available_at) for row in rows]
    result=run(db_session,rows,candidate_id=rows[0].id)
    assert result['status']=='accepted' and result['selected_observation_id']==rows[0].id
    assert result['comparisons'][0]['status']=='incomparable' and result['corroboration_grade']=='not_corroborated'
    issue=db_session.get(ValidationIssue,result['issue_id'])
    kis_input=next(row for row in issue.evidence['source_inputs'] if row['id']==rows[0].id)
    assert kis_input['feed']=='kis:us:'+venue and kis_input['venue']==venue
    assert [(row.id,row.feed,row.venue,row.close,row.captured_at,row.available_at) for row in rows]==original
    assert run(db_session,rows,candidate_id=rows[0].id)['inserted_issues']==0


@pytest.mark.parametrize('venue,feed',[('OTC','kis:us:OTC'),('NAS','kis:us:AMS'),('AMS','sip'),
    ('NAS','kis:us:nas'),('AMS','kis:us:<explicit_exchange>')])
def test_kis_template_unknown_venue_or_inconsistent_native_feed_is_rejected_before_audit(db_session,venue,feed):
    from backtest_data.prices.kis_overseas_storage import KIS_OVERSEAS_SPEC
    sec=security(db_session)
    row=stored(db_session,sec,source='kis_overseas',adjustment='raw',specification=KIS_OVERSEAS_SPEC,
        stored_feed=feed,stored_venue=venue)
    before=db_session.scalar(select(func.count()).select_from(ValidationIssue))
    with pytest.raises(ValueError,match='feed|venue'):run(db_session,[row])
    assert db_session.scalar(select(func.count()).select_from(ValidationIssue))==before


def test_unregistered_kis_feed_template_is_not_resolved_by_generic_placeholder_replacement(db_session):
    from backtest_data.prices.kis_overseas_storage import KIS_OVERSEAS_SPEC
    sec=security(db_session)
    row=stored(db_session,sec,source='kis_overseas',adjustment='raw',
        specification={**KIS_OVERSEAS_SPEC,'feed':'kis:us:<exchange>'},stored_feed='kis:us:AMS',stored_venue='AMS')
    with pytest.raises(ValueError,match='feed'):run(db_session,[row])


def test_october7_prices_captured_october10_are_declaration_visible_at_october8_cutoff(db_session):
    from backtest_data.prices.kis_overseas_storage import KIS_OVERSEAS_SPEC
    sec=security(db_session);day=date(2026,10,7)
    rows=[stored(db_session,sec,source='kis_overseas',adjustment='raw',specification=KIS_OVERSEAS_SPEC,
        stored_feed='kis:us:AMS',stored_venue='AMS',stored_session=day),stored(db_session,sec,stored_session=day)]
    original=[(row.id,row.captured_at,row.available_at) for row in rows]
    result=run(db_session,rows,as_of=datetime(2026,10,8,4,tzinfo=timezone.utc),candidate_id=rows[1].id)
    assert result['status']=='accepted' and result['selected_observation_id']==rows[1].id
    assert result['comparisons'][0]['status']=='incomparable' and result['corroboration_grade']=='not_corroborated'
    assert result['price_capture_pit_required'] is False
    issue=db_session.get(ValidationIssue,result['issue_id'])
    assert issue.evidence['price_capture_pit_required'] is False
    assert [(row.id,row.captured_at,row.available_at) for row in rows]==original


def test_late_capture_policy_still_blocks_uncompleted_sessions_and_backdated_audits(db_session):
    sec=security(db_session);row=stored(db_session,sec,stored_session=date(2026,10,8))
    result=run(db_session,[row],as_of=datetime(2026,10,8,4,tzinfo=timezone.utc))
    assert result['status']=='unavailable' and result['inserted_decisions']==0
    before=db_session.scalar(select(func.count()).select_from(ValidationIssue))
    with pytest.raises(ValueError,match='recording.*capture'):
        run(db_session,[row],as_of=datetime(2026,10,8,4,tzinfo=timezone.utc),recorded_at=STAMP-timedelta(seconds=1))
    assert db_session.scalar(select(func.count()).select_from(ValidationIssue))==before
