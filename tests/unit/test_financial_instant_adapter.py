from datetime import date,datetime,timezone
from decimal import Decimal

import pytest

from backtest_data.fundamentals.models import SourceFinancialFact,FundamentalFact
from backtest_data.fundamentals.materialize import _source_adapter,canonical_to_financial_fact


def inputs():
    now=datetime(2024,1,2,tzinfo=timezone.utc)
    source=SourceFinancialFact(id='S',issuer_id='I',document_id='D',concept='us-gaap:Assets',
        dimensions={},period_kind='instant',period_end=None,instant=date(2023,12,31),scope='consolidated',
        unit='USD',original_unit='USD',value=Decimal('100'),raw_value='100',is_nil=False,
        available_at=now,captured_at=now,first_seen_at=now,extracted_at=now,
        evidence={'taxonomy':'http://fasb.org/us-gaap/2023','parse_status':'ok'})
    canonical=FundamentalFact(id='M',issuer_id='I',source_fact_id='S',metric_id='assets_total',value=Decimal('100'),
        unit='USD',period={'kind':'instant','end':None,'instant':'2023-12-31'},
        basis={'scope':'consolidated','currency':'USD','accounting_basis':'US_GAAP'},
        available_at=now,captured_at=now,materialized_at=now)
    return source,canonical


def test_actual_instant_date_survives_adapter_without_overwriting_source():
    source,canonical=inputs()
    assert _source_adapter(source).period_end==source.instant
    assert canonical_to_financial_fact(canonical,source=source).period_end==source.instant
    assert source.period_end is None


def test_conflicting_instant_dates_are_rejected():
    source,canonical=inputs()
    source.period_end=date(2023,12,30)
    with pytest.raises(ValueError,match='conflicting_instant_source_dates'):_source_adapter(source)
    with pytest.raises(ValueError,match='conflicting_instant_source_dates'):
        canonical_to_financial_fact(canonical,source=source)
