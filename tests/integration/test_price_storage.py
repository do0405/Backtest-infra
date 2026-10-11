from datetime import date, datetime, timezone
from decimal import Decimal
import json
from itertools import permutations

import pytest
from sqlalchemy import select, func

from backtest_data.catalog.models import Security
from backtest_data.ingestion.http import ProviderResponse
from backtest_data.prices.adapters.kis import KISDailyRequest
from backtest_data.prices.storage import store_kis_prices
from backtest_data.prices.models import PriceObservation, VWAPDailyStat


pytestmark = pytest.mark.integration


def test_raw_price_repeats_do_not_overwrite_and_notional_does_not_certify_exact_vwap(db_session, tmp_path):
    security = Security(market="KR", source_identifier="storage-isin", security_type="unclassified", name="Example")
    db_session.add(security)
    db_session.flush()
    captured = datetime(2026, 1, 2, tzinfo=timezone.utc)
    row = {"stck_bsop_date": "20200102", "stck_oprc": "100", "stck_hgpr": "120", "stck_lwpr": "90", "stck_clpr": "110",
           "acml_vol": "100", "acml_tr_pbmn": "10500"}
    payload = {"rt_cd": "0", "output2": [row]}
    response = ProviderResponse(json.dumps(payload).encode(), 200, captured, "application/json")
    request = KISDailyRequest("005930", date(2020, 1, 1), date(2020, 1, 31))
    store_kis_prices(db_session, security.id, request, response, payload, tmp_path)
    store_kis_prices(db_session, security.id, request, response, payload, tmp_path)
    rows = db_session.scalars(select(PriceObservation).where(PriceObservation.security_id == security.id)).all()
    assert len(rows) == 1
    assert rows[0].volume == Decimal(100)
    assert rows[0].notional == Decimal(10500)
    assert rows[0].available_at == captured
    assert db_session.scalar(select(func.count()).select_from(VWAPDailyStat)) == 0


@pytest.mark.parametrize("sequence", [(*order, (4, "110")) for order in permutations(((1, "110"), (2, "111"), (3, "110")))])
def test_capture_events_preserve_source_reversions_and_out_of_order_confirmations(db_session, tmp_path, sequence):
    security = Security(market="KR", source_identifier="reversion-isin", security_type="unclassified", name="Example")
    db_session.add(security)
    db_session.flush()
    request = KISDailyRequest("005930", date(2020, 1, 1), date(2020, 1, 31))
    for day, close in sequence:
        row = {"stck_bsop_date": "20200102", "stck_oprc": "100", "stck_hgpr": "120", "stck_lwpr": "90", "stck_clpr": close, "acml_vol": "100"}
        payload = {"rt_cd": "0", "output2": [row]}
        response = ProviderResponse(json.dumps(payload).encode(), 200, datetime(2026, 1, day, tzinfo=timezone.utc), "application/json")
        store_kis_prices(db_session, security.id, request, response, payload, tmp_path)
    store_kis_prices(db_session, security.id, request, response, payload, tmp_path)
    rows = db_session.scalars(select(PriceObservation).where(PriceObservation.security_id == security.id).order_by(PriceObservation.captured_at)).all()
    assert [row.close for row in rows] == [Decimal(110), Decimal(111), Decimal(110), Decimal(110)]
    assert rows[-1].captured_at.day == 4
