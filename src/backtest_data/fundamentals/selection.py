"""Apply PIT gates before choosing financial revisions."""

from datetime import datetime
from dataclasses import replace
from typing import Iterable

from .domain import FinancialFact, aware


class AmbiguousFinancialRevision(ValueError):
    """A source/decision must resolve equal-rank conflicting observations."""


def fact_visible(fact: FinancialFact, *, as_of: datetime, history_mode: str = 'reconstructed_public') -> bool:
    aware(as_of, 'as_of')
    if history_mode not in {'reconstructed_public', 'observed_system'}:
        raise ValueError(f'unsupported_history_mode:{history_mode}')
    if fact.available_at is None or fact.available_at > as_of:
        return False
    if history_mode == 'observed_system':
        times = (fact.captured_at, fact.first_seen_at, fact.extracted_at, fact.decision_recorded_at)
        if any(value is None or value > as_of for value in times):
            return False
        if fact.materialized_at is not None and fact.materialized_at > as_of:
            return False
        if not fact.directly_reported and fact.materialized_at is None:
            return False
    return True


def fact_identity(fact: FinancialFact) -> tuple:
    return (fact.metric, fact.period_kind, fact.period_start, fact.period_end, fact.unit,
            fact.scope, fact.currency, fact.share_basis, fact.dimensions, fact.fiscal_lineage,
            fact.accounting_basis, fact.release_id)


def select_facts_asof(facts: Iterable[FinancialFact], *, as_of: datetime,
                       history_mode: str = 'reconstructed_public') -> tuple[FinancialFact, ...]:
    selected: dict[tuple, FinancialFact] = {}
    for fact in facts:
        if not fact_visible(fact, as_of=as_of, history_mode=history_mode):
            continue
        key = fact_identity(fact)
        current = selected.get(key)
        if current is not None and (fact.revision,fact.available_at)==(current.revision,current.available_at) and fact.value!=current.value:
            raise AmbiguousFinancialRevision(f'conflicting_revision:{fact.metric}:{fact.period_end}')
        if current is None or (fact.revision, fact.available_at, fact.captured_at, fact.id) > (
                current.revision, current.available_at, current.captured_at, current.id):
            selected[key] = fact
    return tuple(selected.values())


def select_public_facts_asof(facts: Iterable[FinancialFact], *, as_of: datetime,
                       history_mode: str = 'reconstructed_public') -> tuple[FinancialFact, ...]:
    """Eligibility uses its history clock; verified version order uses disclosure.

    User/catalog revision numbers and late capture cannot reorder regulator
    versions. Date-only conflicting versions remain ambiguous in both modes.
    """
    groups={}
    for fact in facts:
        if not fact_visible(fact,as_of=as_of,history_mode=history_mode):continue
        key=fact_identity(fact)
        groups.setdefault(key,[]).append(fact)
    selected=[]
    for versions in groups.values():
        candidates=[]
        for fact in versions:
            public=(fact.evidence.get('public_availability') or {}).get('verified_public_bound')
            if isinstance(public,str):public=datetime.fromisoformat(public)
            candidates.append((public,fact.available_at,fact.value,fact.id))
        index=public_revision_index(candidates)
        if index is None:
            fact=versions[0]
            raise AmbiguousFinancialRevision(f'conflicting_public_bound:{fact.metric}:{fact.period_end}')
        # Downstream calculators use the legacy selector on this already
        # selected set. Numeric revision rank cannot reorder public versions.
        selected.append(replace(versions[index],revision=0))
    return tuple(selected)


def public_revision_index(candidates):
    """Select a latest eligible version, or None if public order is unresolved.

    Each candidate is (verified bound, observed availability, semantic value,
    immutable ID). Apply history eligibility before calling. Only the latest
    certified bound's conflicts matter. A competing capture-only value cannot
    assert public supersession, even if it was collected later. Wholly
    capture-only groups retain their explicitly unqualified observation order.
    """
    verified=[]
    for index,(public,available,_,_) in enumerate(candidates):
        aware(public or available,'verified_public_bound')
        if public is not None:verified.append(index)
    pool=verified or list(range(len(candidates)))
    if not pool:return None
    rank=max(candidates[index][0] or candidates[index][1] for index in pool)
    winning=[index for index in pool if (candidates[index][0] or candidates[index][1])==rank]
    value=candidates[winning[0]][2]
    if any(candidates[index][2]!=value for index in winning):return None
    if verified and any(public is None and candidate_value!=value for public,_,candidate_value,_ in candidates):return None
    return min(winning,key=lambda index:candidates[index][3])
