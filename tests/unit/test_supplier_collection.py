from datetime import date,datetime,timezone
import pytest

def test_supplier_request_keeps_page_anchor_and_declares_adjustment():
    from backtest_data.prices.supplier_collection import SupplierAlpacaRequest
    request=SupplierAlpacaRequest(date(2016,1,1),date(2026,10,7),'AAPL','split','continuation',datetime(2026,10,10,tzinfo=timezone.utc))
    assert request.params()['adjustment']=='split'
    assert request.params()['page_token']=='continuation'
    assert request.params()['limit']=='100'
    assert request.params()['asof']=='-'
    assert request.endpoint()=='/v2/stocks/AAPL/bars'
    with pytest.raises(ValueError):SupplierAlpacaRequest(date(2016,1,1),date(2026,10,7),'AAPL','raw',None,datetime(2026,10,10,tzinfo=timezone.utc))

def test_supplier_symbol_404_is_gap_and_access_error_keeps_cursor():
    from types import SimpleNamespace
    from backtest_data.prices.supplier_collection import SupplierAlpacaRequest,record_supplier_unavailable
    stored=[];session=SimpleNamespace(add=stored.append)
    state=SimpleNamespace(cursor={'member_index':8,'page_token':'old'},status='collecting',updated_at=None)
    request=SupplierAlpacaRequest(date(2016,1,1),date(2026,10,7),'AAPL','split',None,datetime(2026,10,10,tzinfo=timezone.utc))
    assert not record_supplier_unavailable(session,state,{'security_id':'same-target'},request,'401')
    assert state.cursor['member_index']==8 and not stored
    assert record_supplier_unavailable(session,state,{'security_id':'same-target'},request,'404')
    assert state.cursor['member_index']==9 and state.cursor['unsupported_members']==1
    assert stored[0].evidence['security_id']=='same-target' and stored[0].severity=='gap'
