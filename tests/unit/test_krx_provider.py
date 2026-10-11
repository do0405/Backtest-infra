from datetime import date,datetime,timezone
import json

import pytest

from backtest_data.credentials import Credentials,Secret
from backtest_data.ingestion.http import ProviderHTTP,ProviderResponse,ProviderError


def test_registered_krx_production_get_paths_only():
    from backtest_data.providers import KRXData
    transport=ProviderHTTP(None)
    try:
        for path in KRXData.paths.values():
            transport._validate('krx','GET',KRXData.base+path)
        for method,url in [('POST',KRXData.base+'/svc/apis/sto/stk_bydd_trd'),
                           ('GET',KRXData.base+'/svc/apis/not_verified'),
                           ('GET',KRXData.base+'/svc/apis/sto/stk_bydd_trd?AUTH_KEY=never'),
                           ('GET',KRXData.base+':9999/svc/apis/sto/stk_bydd_trd')]:
            with pytest.raises(ValueError):
                transport._validate('krx',method,url)
        assert transport.intervals['krx']>=10
    finally:
        transport.close()


def test_krx_header_only_and_exact_date_parameter():
    from backtest_data.providers import KRXData
    calls=[]
    class HTTP:
        def request(self,*args,**kwargs):
            calls.append((args,kwargs))
            return ProviderResponse(b'{"OutBlock_1":[]}',200,datetime.now(timezone.utc),'application/json')
    credentials=Credentials(*(Secret('') for _ in range(5)),'',Secret('dummy-krx-key'))
    response,payload=KRXData(HTTP(),credentials).daily_quotes('etf',date(2020,1,2))
    args,kwargs=calls[0]
    assert args==('krx','GET','https://data-dbg.krx.co.kr/svc/apis/etp/etf_bydd_trd')
    assert kwargs['params']=={'basDd':'20200102'}
    assert kwargs['headers']=={'AUTH_KEY':'dummy-krx-key'}
    assert 'dummy-krx-key' not in args[2] and payload=={'OutBlock_1':[]}


@pytest.mark.parametrize('service,day',[('unknown',date(2020,1,2)),('kospi',date(2100,1,1)),('kospi','20200102')])
def test_krx_invalid_inputs_fail_before_auth_or_transport(service,day):
    from backtest_data.providers import KRXData
    class HTTP:
        def request(self,*args,**kwargs):
            pytest.fail('invalid inputs must never reach transport')
    credentials=Credentials(*(Secret('') for _ in range(5)),'')
    with pytest.raises(ValueError):
        KRXData(HTTP(),credentials).daily_quotes(service,day)
