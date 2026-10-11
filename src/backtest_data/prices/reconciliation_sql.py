"""Bounded, append-only SQL adapter for supplier price comparisons.

Callers own the transaction. This module never updates source observations or
prior selections, and never publishes a release or certifies action coverage.
"""
from dataclasses import asdict
from datetime import date,datetime,timezone
from decimal import Decimal
from uuid import NAMESPACE_URL,uuid5
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from ..catalog.models import Security,SourceContract
from ..releases.models import SelectionDecision,ValidationIssue
from .models import PriceObservation
from .provider_basis import contract_price_basis
from .reconciliation import MAX_CANDIDATES,POLICY,SupplierPriceCandidate,reconcile_supplier_prices
from .validation import ComparisonContract


def _json(value):
    if value is None or isinstance(value,(str,bool,int)):return value
    if isinstance(value,Decimal):return str(value)
    if isinstance(value,datetime):return value.astimezone(timezone.utc).isoformat()
    if isinstance(value,date):return value.isoformat()
    if isinstance(value,(tuple,list)):return [_json(item) for item in value]
    if isinstance(value,dict):return {key:_json(item) for key,item in value.items()}
    raise ValueError('unsupported audit value; exact strings and numbers required')


def _comparison_feed(source,label):
    # These two spellings identify the same approved Alpaca SIP API feed.
    # Prefix stripping would wrongly equate IEX or another supplier's feed.
    if source=='alpaca':
        if label not in ('sip','alpaca:sip'):raise ValueError('stored feed contradicts approved Alpaca SIP contract')
        return 'sip'
    return label


def candidate_from_sql(row:PriceObservation,contract:SourceContract,security:Security):
    """Use API flags and explicit comparison metadata, never a capture anchor.

    session_scope, adjustment_reference_basis, adjustment_reference_evidence_ref,
    upstream_id, and processing_path come from row evidence or contract spec.
    Missing session/reference/provenance metadata remains unknown.
    """
    if contract.id!=row.source_contract_id or security.id!=row.security_id:
        raise ValueError('source/security identity mismatch')
    if contract.domain!='prices' or contract.market!=security.market:
        raise ValueError('unsupported price contract market/domain')
    definition=contract_price_basis(contract)
    if row.basis!=definition['basis']:raise ValueError('stored basis contradicts supplier API declaration')
    spec=contract.specification;evidence=row.evidence or {}
    comparison_feed=_comparison_feed(contract.source,row.feed)
    declared_feed=spec.get('feed')
    if contract.source=='kis_overseas' and declared_feed=='kis:us:<explicit_exchange>':
        # The registered daily-price contract delegates its feed to an explicit
        # US exchange. This is KIS exchange data, never an alias for SIP.
        allowed=('NAS','NYS','AMS')
        venues=spec.get('venues')
        if (row.venue not in allowed or row.feed!='kis:us:'+row.venue
            or venues is not None and (not isinstance(venues,list) or row.venue not in venues)):
            raise ValueError('stored feed or venue contradicts explicit KIS US exchange contract')
        declared_feed='kis:us:'+row.venue
    if declared_feed is not None and _comparison_feed(contract.source,declared_feed)!=comparison_feed:
        raise ValueError('stored feed contradicts explicit supplier contract')
    for field in ('currency','quantity_unit'):
        if spec.get(field) is not None and spec[field]!=getattr(row,field):
            raise ValueError(f'stored {field} contradicts explicit supplier contract')
    def explicit(key):return evidence.get(key,spec.get(key))
    price_unit=explicit('price_unit')
    # Registered stock endpoints express monetary prices per share; quantity
    # must also declare shares. No inference is made for unknown endpoints.
    if price_unit is None and row.quantity_unit=='shares':price_unit='currency_per_share'
    return SupplierPriceCandidate(id=row.id,security_id=row.security_id,market=security.market,
        session=row.session,currency=row.currency,feed=comparison_feed,venue=row.venue,
        session_scope=explicit('session_scope'),trade_scope=row.trade_scope,price_unit=price_unit,
        quantity_unit=row.quantity_unit,source=contract.source,source_contract_id=contract.id,raw_ref=row.raw_id,
        definition=definition,reference_basis=explicit('adjustment_reference_basis'),
        reference_evidence_ref=explicit('adjustment_reference_evidence_ref'),open=row.open,high=row.high,
        low=row.low,close=row.close,volume=row.volume,captured_at=row.captured_at,available_at=row.available_at,
        upstream_id=explicit('upstream_id'),processing_path=explicit('processing_path'))


def reconcile_stored_supplier_prices(session,observation_ids,*,as_of:datetime,recorded_at:datetime,
    contract:ComparisonContract,candidate_id=None,source_priority=(),persist=True,actor='supplier_reconciliation'):
    """Compare up to 64 exact stored IDs; optionally append audit/nomination.

    Quarantined/incomparable peers do not rewrite prior supplier-declaration
    selections. An explicit candidate can be nominated by its API declaration
    when peers are incomparable; this produces no corroboration claim.
    Replays retain the original audit and decision clocks. Commit is the caller's.
    """
    ids=tuple(observation_ids)
    if (not 1<=len(ids)<=MAX_CANDIDATES or any(not isinstance(identity,str) or not identity for identity in ids)
        or len(set(ids))!=len(ids)):raise ValueError('bounded unique stored observation IDs required')
    if not isinstance(actor,str) or not actor or len(actor)>120:raise ValueError('bounded audit actor required')
    rows=session.execute(select(PriceObservation,SourceContract,Security)
        .join(SourceContract,SourceContract.id==PriceObservation.source_contract_id)
        .join(Security,Security.id==PriceObservation.security_id).where(PriceObservation.id.in_(ids))).all()
    if {row[0].id for row in rows}!=set(ids):raise ValueError('requested stored price observation missing')
    candidates=tuple(candidate_from_sql(*row) for row in rows)
    result=reconcile_supplier_prices(candidates,as_of=as_of,recorded_at=recorded_at,contract=contract,
        candidate_id=candidate_id,source_priority=source_priority)
    decision=result.decision;issue_id=str(uuid5(NAMESPACE_URL,decision.id+':audit'))
    comparisons=[_json(asdict(pair)) for pair in result.comparisons]
    evidence={'policy':POLICY,'as_of':_json(as_of),'status':decision.status,'reason':decision.reason,
        'selected_observation_id':decision.selected_observation_id,'observation_ids':list(decision.observation_ids),
        'declaration_grade':result.declaration_grade,'corroboration_grade':result.corroboration_grade,
        'agreeing_observation_ids':list(result.agreeing_observation_ids),
        'dissenting_observation_ids':list(result.dissenting_observation_ids),
        'excluded_observation_ids':list(result.excluded_observation_ids),'comparison_contract':_json(asdict(contract)),
        'source_priority':list(source_priority),'requested_candidate_id':candidate_id,'comparisons':comparisons,
        'comparison_fields':['open','high','low','close'],'quantity_corroborated':False,'matched_vwap':False,
        'price_capture_pit_required':False,
        'corporate_action_role':'supplemental','source_inputs':[_json(asdict(row)) for row in sorted(candidates,key=lambda row:row.id)]}
    feeds={candidate.id:candidate.feed for candidate in candidates}
    normalized=[{'observation_id':row.id,'stored_feed':row.feed,'comparison_feed':feeds[row.id],
        'policy':'alpaca_sip_feed_label_alias_v1'} for row,_,_ in sorted(rows,key=lambda entry:entry[0].id)
        if row.feed!=feeds[row.id]]
    if normalized:evidence['feed_normalization']=normalized
    inserted_issues=inserted_decisions=0;receipt_clock=recorded_at
    if persist:
        # One savepoint prevents a decision from surviving a failed audit write.
        with session.begin_nested():
            issue=session.get(ValidationIssue,issue_id)
            if issue is None:
                inserted=session.execute(insert(ValidationIssue).values(id=issue_id,record_domain='price_observation',
                    record_id=decision.selected_observation_id,rule=POLICY,
                    severity='info' if decision.status=='accepted' and not result.dissenting_observation_ids else 'warning',
                    evidence=evidence,created_at=recorded_at).on_conflict_do_nothing(index_elements=[ValidationIssue.id]).returning(ValidationIssue.id)).scalar_one_or_none()
                inserted_issues=int(inserted is not None)
                issue=session.get(ValidationIssue,issue_id)
            if issue.evidence!=evidence:raise ValueError('reconciliation audit identity conflict')
            receipt_clock=issue.created_at
            if decision.selected_observation_id is not None:
                inserted=session.execute(insert(SelectionDecision).values(id=decision.id,record_domain='price_observation',
                    record_id=decision.selected_observation_id,status=decision.status,rule_version=POLICY,
                    evidence_grade='supplier_declared',reason=decision.reason,actor=actor,recorded_at=receipt_clock)
                    .on_conflict_do_nothing(index_elements=[SelectionDecision.id]).returning(SelectionDecision.id)).scalar_one_or_none()
                inserted_decisions=int(inserted is not None)
                prior=session.get(SelectionDecision,decision.id)
                if (prior.record_domain,prior.record_id,prior.status,prior.rule_version,prior.evidence_grade,prior.reason)!=(
                    'price_observation',decision.selected_observation_id,decision.status,POLICY,'supplier_declared',decision.reason):
                    raise ValueError('reconciliation selection identity conflict')
                receipt_clock=prior.recorded_at
    return {'policy':POLICY,'decision_id':decision.id,'issue_id':issue_id,'status':decision.status,'reason':decision.reason,
        'selected_observation_id':decision.selected_observation_id,'declaration_grade':result.declaration_grade,
        'corroboration_grade':result.corroboration_grade,'agreeing_observation_ids':list(result.agreeing_observation_ids),
        'dissenting_observation_ids':list(result.dissenting_observation_ids),'excluded_observation_ids':list(result.excluded_observation_ids),
        'comparisons':comparisons,'inserted_issues':inserted_issues,'inserted_decisions':inserted_decisions,
        'recorded_at':receipt_clock,'as_of':as_of,'price_capture_pit_required':False}
