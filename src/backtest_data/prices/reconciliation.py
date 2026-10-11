"""Supplier-declared prices with explicit, non-averaging corroboration.

This policy is separate from strict action-ledger price validation/selection.
API declaration may qualify one existing input; unknown adjustment references
or session definitions never become comparable merely because values match.
"""
from dataclasses import dataclass
from datetime import date,datetime
from decimal import Decimal
from itertools import combinations
from uuid import NAMESPACE_URL,uuid5
from .domain import finite_decimal,require_aware
from .numeric import exact_difference,exact_sum,fixed_divide,numeric_signature
from .provider_basis import POLICY as DECLARATION_POLICY,supplier_daily_visible
from .selection import PriceSelectionDecision
from .validation import ComparisonContract,PriceIssue

POLICY='supplier_price_reconciliation_v2'
MAX_CANDIDATES=64


@dataclass(frozen=True,slots=True)
class SupplierPriceCandidate:
    id:str
    security_id:str
    market:str
    session:date
    currency:str
    feed:str
    venue:str
    session_scope:str|None
    trade_scope:str
    price_unit:str
    quantity_unit:str
    source:str
    source_contract_id:str
    raw_ref:str
    definition:dict
    reference_basis:str|None
    reference_evidence_ref:str|None
    open:Decimal
    high:Decimal
    low:Decimal
    close:Decimal
    volume:Decimal|None
    captured_at:datetime
    available_at:datetime|None
    upstream_id:str|None=None
    processing_path:str|None=None


@dataclass(frozen=True,slots=True)
class SupplierComparison:
    status:str
    observation_ids:tuple[str,str]
    issues:tuple[PriceIssue,...]
    independence:str
    evidence_ref:str
    price_tolerance:Decimal|None


@dataclass(frozen=True,slots=True)
class SupplierReconciliation:
    decision:PriceSelectionDecision
    comparisons:tuple[SupplierComparison,...]
    declaration_grade:str
    corroboration_grade:str
    agreeing_observation_ids:tuple[str,...]
    dissenting_observation_ids:tuple[str,...]
    excluded_observation_ids:tuple[str,...]


def _structural(candidate):
    issues=[]
    for field in ('id','security_id','market','currency','feed','venue','trade_scope','price_unit','quantity_unit','source','source_contract_id','raw_ref'):
        if not isinstance(getattr(candidate,field),str) or not getattr(candidate,field):
            issues.append(PriceIssue('missing_context',field,'explicit source context required'))
    definition=candidate.definition
    if (not isinstance(definition,dict) or definition.get('policy')!=DECLARATION_POLICY
        or definition.get('evidence_grade')!='supplier_declared' or definition.get('source')!=candidate.source
        or definition.get('basis') not in {'as_traded','provider_adjusted'}):
        issues.append(PriceIssue('invalid_supplier_declaration','definition','registered API declaration required'))
    else:
        adjustments=definition.get('adjustments')
        allowed={'split','dividend','spin-off','provider_unspecified_actions'}
        if (not isinstance(adjustments,list) or any(not isinstance(value,str) or value not in allowed for value in adjustments)
            or len(set(adjustments))!=len(adjustments)
            or bool(adjustments)!=(definition['basis']=='provider_adjusted')):
            issues.append(PriceIssue('invalid_supplier_declaration','adjustments','explicit consistent adjustment definition required'))
    for field in ('open','high','low','close'):
        value=getattr(candidate,field)
        if not isinstance(value,Decimal) or not value.is_finite() or value<=0:
            issues.append(PriceIssue('invalid_price',field,'positive exact supplier OHLC required'))
    if not issues and not candidate.low<=min(candidate.open,candidate.close)<=max(candidate.open,candidate.close)<=candidate.high:
        issues.append(PriceIssue('invalid_ohlc','ohlc','supplier OHLC ordering failed'))
    try:finite_decimal(candidate.volume,'volume')
    except ValueError:issues.append(PriceIssue('invalid_quantity','volume','finite nonnegative exact quantity required'))
    if type(candidate.session) is not date:issues.append(PriceIssue('invalid_session','session','exact source session date required'))
    if candidate.market not in {'KR','US'}:issues.append(PriceIssue('unsupported_market','market','registered daily market required'))
    for field in ('captured_at','available_at'):
        value=getattr(candidate,field)
        if (field=='captured_at' and not isinstance(value,datetime)) or (value is not None and not isinstance(value,datetime)):
            issues.append(PriceIssue('invalid_clock',field,'aware original source clock required'))
        elif value is not None:
            try:require_aware(value,field)
            except ValueError:issues.append(PriceIssue('invalid_clock',field,'aware original source clock required'))
    return tuple(issues)


def _visible(candidate,as_of):
    # Approved daily price research does not require capture/publication PIT.
    # Original clocks are retained; only the completed market day gates price
    # visibility. Physical audit chronology is checked separately below.
    return not _structural(candidate) and supplier_daily_visible(candidate.session,candidate.market,as_of)


def _definition_key(candidate):
    definition=candidate.definition;adjustments=tuple(sorted(definition['adjustments']))
    unspecified=(candidate.source,definition.get('endpoint'),definition.get('request_flag')) if 'provider_unspecified_actions' in adjustments else None
    return (definition['basis'],adjustments,definition.get('volume_basis'),unspecified)


def _context_key(candidate):
    return (candidate.security_id,candidate.market,candidate.session,candidate.currency,candidate.feed,candidate.venue,
        candidate.session_scope,candidate.trade_scope,candidate.price_unit,candidate.quantity_unit,_definition_key(candidate))


def _independence(left,right,contract):
    if left.raw_ref==right.raw_ref:return 'same_capture'
    if left.processing_path and left.processing_path==right.processing_path:return 'shared_processing'
    if not contract.independent_paths_verified:return 'unknown'
    if (left.source_contract_id==right.source_contract_id or not all((left.upstream_id,right.upstream_id,left.processing_path,right.processing_path))):return 'unknown'
    return 'independent_processing_shared_origin' if left.upstream_id==right.upstream_id else 'independent_paths'


def _explicit(value):
    return (isinstance(value,str) and bool(value.strip()) and value.lower() not in {'unknown','unspecified','unverified'}
        and not value.lower().endswith(('_unknown','_unverified','_unspecified')))


def _clock(value,field):
    if not isinstance(value,datetime):raise ValueError(f'{field} requires an aware timestamp')
    require_aware(value,field)


def compare_supplier_prices(left,right,*,as_of,contract:ComparisonContract,compare_volume=False):
    _clock(as_of,'as_of')
    independence=_independence(left,right,contract);ids=(left.id,right.id)
    tolerance=contract.tick_size
    if contract.left_price_quantum is not None and contract.right_price_quantum is not None:
        tolerance=max(tolerance or Decimal(0),fixed_divide(exact_sum((contract.left_price_quantum,contract.right_price_quantum)),Decimal(2)))
    def result(status,issues):return SupplierComparison(status,ids,tuple(issues),independence,contract.evidence_ref,tolerance)
    errors=_structural(left)+_structural(right)
    if errors:return result('unresolved',errors)
    if not _visible(left,as_of) or not _visible(right,as_of):return result('unavailable',[PriceIssue('uncompleted_market_session','session','market session not completed at cutoff')])
    issues=[]
    if _definition_key(left)!=_definition_key(right):issues.append(PriceIssue('adjustment_definition_mismatch','definition','supplier adjustment definitions differ'))
    if _context_key(left)[:-1]!=_context_key(right)[:-1]:issues.append(PriceIssue('incompatible_context','context','security/session/feed/currency/venue/units differ'))
    if not all(_explicit(value) for value in (left.session_scope,right.session_scope,left.trade_scope,right.trade_scope)):
        issues.append(PriceIssue('unknown_session_scope','session_scope','unknown session/eligibility scope cannot corroborate'))
    if left.definition['basis']=='provider_adjusted' or right.definition['basis']=='provider_adjusted':
        if not all(_explicit(value) for value in (left.reference_basis,right.reference_basis,left.reference_evidence_ref,right.reference_evidence_ref)):
            issues.append(PriceIssue('unknown_adjustment_reference','reference_basis','unknown reference remains incomparable'))
        elif left.reference_basis!=right.reference_basis:issues.append(PriceIssue('adjustment_reference_mismatch','reference_basis','adjustment reference bases differ'))
    if issues:return result('incomparable',issues)
    mismatch=False
    for field in ('open','high','low','close'):
        first,second=getattr(left,field),getattr(right,field)
        if first==second:continue
        if tolerance is None:issues.append(PriceIssue('unverified_price_precision',field,'explicit tick or both source quanta required'))
        elif exact_difference(first,second).copy_abs()>tolerance:
            mismatch=True;issues.append(PriceIssue('price_mismatch',field,'verified absolute tolerance exceeded'))
    if compare_volume:
        if left.volume is None or right.volume is None:issues.append(PriceIssue('missing_quantity','volume','absent quantity remains unknown'))
        elif left.volume!=right.volume:
            if contract.quantity_tolerance is None:issues.append(PriceIssue('unverified_quantity_precision','volume','explicit quantity tolerance required'))
            elif exact_difference(left.volume,right.volume).copy_abs()>contract.quantity_tolerance:
                mismatch=True;issues.append(PriceIssue('quantity_mismatch','volume','quantity tolerance exceeded'))
    return result('mismatch' if mismatch else 'unresolved' if issues else 'agree',issues)


def reconcile_supplier_prices(candidates,*,as_of,recorded_at,contract:ComparisonContract,candidate_id=None,source_priority=()):
    _clock(as_of,'as_of');_clock(recorded_at,'recorded_at')
    if as_of>recorded_at:raise ValueError('future query cutoff exceeds recording clock')
    inputs=tuple(candidates)
    if not 1<=len(inputs)<=MAX_CANDIDATES or len({row.id for row in inputs})!=len(inputs):raise ValueError('bounded unique candidate IDs required')
    if candidate_id is not None and candidate_id not in {row.id for row in inputs}:raise ValueError('requested candidate ID absent')
    if any(not _structural(row) and row.captured_at>recorded_at for row in inputs):
        raise ValueError('recording clock precedes input capture')
    visible=[row for row in inputs if _visible(row,as_of)];groups={}
    for row in visible:
        reference=row.reference_basis if row.definition['basis']=='provider_adjusted' else None
        groups.setdefault((row.source_contract_id,_context_key(row),reference),[]).append(row)
    current=[];revision_conflict=False
    for group in groups.values():
        latest=max(row.captured_at for row in group);tied=[row for row in group if row.captured_at==latest]
        if len({(row.open,row.high,row.low,row.close,row.volume) for row in tied})>1:revision_conflict=True
        current.append(min(tied,key=lambda row:row.id))
    current.sort(key=lambda row:row.id)
    comparisons=tuple(compare_supplier_prices(left,right,as_of=as_of,contract=contract) for left,right in combinations(current,2))
    by_id={row.id:row for row in current};neighbors={row.id:set() for row in current}
    for pair in comparisons:
        if pair.status=='agree':
            left,right=pair.observation_ids;neighbors[left].add(right);neighbors[right].add(left)
    components=[];remaining=set(neighbors)
    while remaining:
        start=min(remaining);component={start};pending=[start]
        while pending:
            for linked in neighbors[pending.pop()]-component:component.add(linked);pending.append(linked)
        remaining-=component
        if len(component)>1:components.append(component)
    nontransitive=any(any(second not in neighbors[first] for first,second in combinations(sorted(group),2)) for group in components)
    status='accepted';reason='supplier_declaration_only';agreeing=();dissent=();grade='not_corroborated';selected=None
    ranks={source:rank for rank,source in enumerate(source_priority)}
    def preference(row):return (ranks.get(row.source,len(ranks)),row.source,row.source_contract_id,row.id)
    if not current:status,reason='unavailable','no_visible_declared_supplier_value'
    elif revision_conflict:status,reason='quarantined','conflicting_same_capture_revision'
    elif nontransitive or len(components)>1:status,reason='quarantined','ambiguous_agreement_groups'
    elif components:
        group=components[0]
        if candidate_id is not None and candidate_id not in group:status,reason='quarantined','requested_candidate_outside_unique_agreement'
        else:
            selected=by_id[candidate_id] if candidate_id else min((by_id[identity] for identity in group),key=preference)
            agreeing=tuple(sorted(group));reason='unique_same_definition_agreement'
            dissent=tuple(sorted({identity for pair in comparisons if pair.status=='mismatch' and set(pair.observation_ids)&group for identity in set(pair.observation_ids)-group}))
            paired=[pair for pair in comparisons if pair.status=='agree' and set(pair.observation_ids)<=group]
            grade=('corroborated_independent_paths' if any(pair.independence in {'independent_paths','independent_processing_shared_origin'} for pair in paired)
                else 'agreement_shared_provenance' if all(pair.independence!='unknown' for pair in paired) else 'agreement_independence_unknown')
    elif candidate_id is not None:
        selected=by_id.get(candidate_id)
        if selected is None:status,reason='unavailable','requested_candidate_not_current_or_visible'
        elif any(pair.status in {'mismatch','unresolved'} and candidate_id in pair.observation_ids for pair in comparisons):status,reason,selected='quarantined','unresolved_candidate_comparison',None
    elif len(current)==1:selected=current[0]
    else:status,reason='quarantined','no_unique_comparable_agreement'
    all_ids=tuple(sorted(row.id for row in inputs));excluded=tuple(sorted(set(all_ids)-set(by_id)))
    # Source values and clocks participate in the audit identity; IDs alone do
    # not permit a changed input to reuse an earlier immutable audit receipt.
    revisions=tuple((row.id,_context_key(row),row.raw_ref,row.reference_basis,row.reference_evidence_ref,
        row.open,row.high,row.low,row.close,row.volume,row.captured_at,row.available_at,row.upstream_id,row.processing_path)
        for row in sorted(visible,key=lambda row:row.id))
    identity=numeric_signature(POLICY,all_ids,revisions,as_of,contract,candidate_id,tuple(source_priority))
    decision=PriceSelectionDecision(str(uuid5(NAMESPACE_URL,identity)),selected.id if selected else None,all_ids,(),POLICY,reason,
        'supplier_declared' if selected else None,recorded_at,status)
    return SupplierReconciliation(decision,comparisons,'supplier_declared',grade,agreeing,dissent,excluded)
