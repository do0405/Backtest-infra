from datetime import date,datetime,timezone
import pytest

def test_kis_flags_are_endpoint_specific_and_need_no_action_history():
    from backtest_data.prices.provider_basis import classify_price_basis
    domestic='/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice'
    overseas='/uapi/overseas-price/v1/quotations/dailyprice'
    assert classify_price_basis('kis',domestic,{'FID_ORG_ADJ_PRC':'1'})['basis']=='as_traded'
    assert classify_price_basis('kis',domestic,{'FID_ORG_ADJ_PRC':'0'})['basis']=='provider_adjusted'
    assert classify_price_basis('kis_overseas',overseas,{'MODP':'0'})['basis']=='as_traded'
    assert classify_price_basis('kis_overseas',overseas,{'MODP':'1'})['basis']=='provider_adjusted'
    assert not classify_price_basis('kis',domestic,{'FID_ORG_ADJ_PRC':'0'})['corporate_action_completeness_required']

def test_alpaca_scope_and_volume_are_separate():
    from backtest_data.prices.provider_basis import classify_price_basis
    endpoint='/v2/stocks/AAPL/bars'
    split=classify_price_basis('alpaca',endpoint,{'adjustment':'split','asof':'2016-01-01'})
    assert split['adjustments']==['split'] and split['volume_basis']=='split_adjusted'
    assert split['adjustment_reference_date'] is None # asof maps symbols only
    dividend=classify_price_basis('alpaca',endpoint,{'adjustment':'dividend'})
    assert dividend['volume_basis']=='as_traded' and dividend['basis']=='provider_adjusted'
    with pytest.raises(ValueError):classify_price_basis('alpaca',endpoint,{'adjustment':'invented'})

def test_daily_supplier_classification_does_not_allow_future_bars():
    from backtest_data.prices.provider_basis import supplier_daily_visible
    cutoff=datetime(2026,10,8,4,tzinfo=timezone.utc)
    assert supplier_daily_visible(date(2026,10,7),'US',cutoff)
    assert not supplier_daily_visible(date(2026,10,8),'US',cutoff)
