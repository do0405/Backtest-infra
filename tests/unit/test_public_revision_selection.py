from dataclasses import replace
from datetime import date,datetime,timedelta,timezone
from decimal import Decimal
from itertools import permutations

import pytest

from backtest_data.fundamentals.domain import FinancialFact
from backtest_data.fundamentals.selection import AmbiguousFinancialRevision

BOUND=datetime(2024,3,2,5,tzinfo=timezone.utc)
CAPTURE=datetime(2026,10,1,tzinfo=timezone.utc)


def fact(id,value,public,*,captured=CAPTURE,revision=0):
    return FinancialFact(id=id,metric='revenue',value=Decimal(value),period_kind='annual',period_start=date(2023,1,1),
        period_end=date(2023,12,31),unit='USD',scope='consolidated',available_at=public,captured_at=captured,
        first_seen_at=captured,extracted_at=captured,materialized_at=captured,decision_recorded_at=captured,
        revision=revision,evidence={'public_availability':{'verified_public_bound':public}})


def test_public_revision_numbers_cannot_override_verified_public_order():
    from backtest_data.fundamentals.selection import select_public_facts_asof
    earlier=fact('O','100',BOUND,revision=9)
    later=fact('A','110',BOUND+timedelta(days=4),revision=0)
    assert select_public_facts_asof([earlier,later],as_of=BOUND+timedelta(days=5))[0].id=='A'
    with pytest.raises(AmbiguousFinancialRevision):
        select_public_facts_asof([earlier,replace(later,available_at=BOUND,evidence=earlier.evidence)],as_of=BOUND)


def test_observed_eligibility_keeps_capture_but_orders_known_versions_by_public_bound():
    from backtest_data.fundamentals.selection import select_public_facts_asof
    original=fact('O','100',BOUND,captured=CAPTURE+timedelta(days=3))
    correction=fact('A','110',BOUND+timedelta(days=4),captured=CAPTURE)
    original=replace(original,available_at=original.captured_at)
    correction=replace(correction,available_at=correction.captured_at)
    assert select_public_facts_asof([original,correction],as_of=CAPTURE+timedelta(days=4),history_mode='observed_system')[0].id=='A'


@pytest.mark.parametrize('order',list(permutations(('old1','old2','new'))))
@pytest.mark.parametrize('mode',('reconstructed_public','observed_system'))
def test_superseded_public_conflict_does_not_suppress_later_verified_version(order,mode):
    from backtest_data.fundamentals.selection import select_public_facts_asof
    versions={'old1':fact('old1','100',BOUND),'old2':fact('old2','110',BOUND),
        'new':fact('new','120',BOUND+timedelta(days=5))}
    cutoff=CAPTURE+timedelta(days=1) if mode=='observed_system' else BOUND+timedelta(days=6)
    selected=select_public_facts_asof([versions[key] for key in order],as_of=cutoff,history_mode=mode)
    assert [row.id for row in selected]==['new']


def test_public_conflict_remains_unavailable_until_later_version_is_public():
    from backtest_data.fundamentals.selection import select_public_facts_asof
    with pytest.raises(AmbiguousFinancialRevision):
        select_public_facts_asof([fact('old1','100',BOUND),fact('old2','110',BOUND),
            fact('new','120',BOUND+timedelta(days=5))],as_of=BOUND+timedelta(days=3))


@pytest.mark.parametrize('mode',('reconstructed_public','observed_system'))
def test_capture_only_old_value_cannot_supersede_verified_correction(mode):
    from backtest_data.fundamentals.selection import select_public_facts_asof
    original=replace(fact('O','100',CAPTURE),evidence={'public_availability':{'status':'capture_only','verified_public_bound':None}})
    correction=fact('A','110',BOUND+timedelta(days=4),captured=BOUND+timedelta(days=4))
    assert select_public_facts_asof([original,correction],as_of=BOUND+timedelta(days=5),history_mode=mode)[0].id=='A'
    for versions in ([original,correction],[correction,original]):
        with pytest.raises(AmbiguousFinancialRevision):
            select_public_facts_asof(versions,as_of=CAPTURE+timedelta(days=1),history_mode=mode)


def test_capture_only_equal_value_retains_certified_version():
    from backtest_data.fundamentals.selection import select_public_facts_asof
    unknown=replace(fact('O','110',CAPTURE),evidence={'public_availability':{'status':'capture_only','verified_public_bound':None}})
    correction=fact('A','110',BOUND+timedelta(days=4))
    assert select_public_facts_asof([unknown,correction],as_of=CAPTURE+timedelta(days=1))[0].id=='A'
