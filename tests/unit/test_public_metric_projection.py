from datetime import date,datetime,timezone
from decimal import Decimal

import pytest

from backtest_data.fundamentals.models import FundamentalFact


def test_metric_adapter_projects_public_clock_and_keeps_physical_metadata():
    from backtest_data.fundamentals.materialize import canonical_to_financial_fact
    public=datetime(2024,3,2,5,tzinfo=timezone.utc)
    capture=datetime(2026,10,1,tzinfo=timezone.utc)
    row=FundamentalFact(id='M',issuer_id='I',source_fact_id='S',metric_id='revenue',value=Decimal('100'),unit='USD',
        period={'kind':'duration','start':'2023-01-01','end':'2023-12-31'},basis={},
        available_at=capture,captured_at=capture,materialized_at=capture)
    resolution={'effective_available_at':public,'status':'qualified','evidence_ids':['E'],'policy_version':'financial_public_pit_v1'}
    projected=canonical_to_financial_fact(row,availability=resolution)
    assert projected.available_at==public
    assert projected.captured_at==projected.materialized_at==capture
    assert projected.evidence['public_availability']==resolution
    assert row.available_at==capture
    assert canonical_to_financial_fact(row).available_at==capture


def test_public_metric_request_rejects_unknown_version_before_any_sql_write():
    from backtest_data.fundamentals.materialize import materialize_metrics
    with pytest.raises(ValueError,match='unsupported_policy_version'):
        materialize_metrics(None,as_of=datetime(2024,1,1,tzinfo=timezone.utc),query_version='future')
