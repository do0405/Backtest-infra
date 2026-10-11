from datetime import date, datetime, timezone
import json

import pytest

from backtest_data.credentials import Credentials, Secret
from backtest_data.ingestion.http import ProviderResponse
from backtest_data.ingestion.http import ProviderError
from backtest_data.db import utcnow
from backtest_data.prices.adapters.kis import KISDailyRequest
from backtest_data.providers import KISQuotes, OpenDART
from backtest_data.prices.adapters.kis_overseas import KISOverseasDailyRequest


class FakeHTTP:
    def __init__(self):
        self.requests = []

    def request(self, source, method, url, **kwargs):
        self.requests.append((source, method, url, kwargs))
        payload = {"access_token": "dummy-token", "expires_in": 3600} if url.endswith("tokenP") else {"rt_cd": "0", "output2": []}
        return ProviderResponse(json.dumps(payload).encode(), 200, utcnow(), "application/json")


def test_quotes_use_endpoint_specific_raw_option_and_do_not_persist_token():
    http = FakeHTTP()
    credentials = Credentials(Secret("dummy-key"), Secret("dummy-secret"), Secret(""), Secret(""), Secret(""), "")
    client = KISQuotes(http, credentials)
    request = KISDailyRequest("005930", date(2020, 1, 1), date(2020, 1, 31))
    client.daily_prices(request)
    client.daily_prices(request)
    assert len([r for r in http.requests if r[1] == "POST"]) == 1
    assert http.requests[-1][3]["params"]["FID_ORG_ADJ_PRC"] == "1"


def test_dart_unregistered_endpoint_is_rejected():
    client = OpenDART(FakeHTTP(), Credentials(*(Secret("") for _ in range(5)), ""))
    with pytest.raises(ValueError, match="Unregistered"):
        client.fetch("unexpected")


def test_kis_us_uses_existing_auth_and_a_separate_raw_option():
    http = FakeHTTP()
    credentials = Credentials(Secret('dummy-key'),Secret('dummy-secret'),Secret(''),Secret(''),Secret(''),'')
    client = KISQuotes(http,credentials)
    client.daily_prices(KISDailyRequest('005930',date(2020,1,1),date(2020,1,31)))
    request = KISOverseasDailyRequest('AAPL',date(2020,1,1),date(2020,1,31),'NAS',
        captured_at=datetime(2020,2,1,12,tzinfo=timezone.utc))
    client.overseas_daily_prices(request)
    assert len([row for row in http.requests if row[1]=='POST'])==1
    call = http.requests[-1]
    assert call[0]=='kis'
    assert call[2].endswith('/overseas-price/v1/quotations/dailyprice')
    assert call[3]['headers']['tr_id']=='HHDFS76240000'
    assert call[3]['params']['MODP']=='0'


@pytest.mark.parametrize('code', ['EGW00201', 'EGW00133'])
def test_domestic_kis_throttles_are_retryable(code):
    class HTTP(FakeHTTP):
        def request(self, source, method, url, **kwargs):
            if url.endswith('tokenP'):
                return super().request(source, method, url, **kwargs)
            return ProviderResponse(json.dumps({'rt_cd':'1','msg_cd':code}).encode(), 200, utcnow(), 'application/json')
    credentials = Credentials(Secret('dummy-key'),Secret('dummy-secret'),Secret(''),Secret(''),Secret(''),'')
    with pytest.raises(ProviderError) as error:
        KISQuotes(HTTP(), credentials).daily_prices(KISDailyRequest('005930', date(2020,1,1), date(2020,1,31)))
    assert error.value.retryable is True


@pytest.mark.parametrize('code', ['800', '900'])
def test_dart_json_maintenance_and_temporary_errors_are_retryable(code):
    class HTTP:
        def request(self, *args, **kwargs):
            return ProviderResponse(json.dumps({'status':code}).encode(), 200, utcnow(), 'application/json')
    credentials = Credentials(Secret(''),Secret(''),Secret('dart-key'),Secret(''),Secret(''),'')
    with pytest.raises(ProviderError) as error:
        OpenDART(HTTP(), credentials).fetch('list.json')
    assert error.value.retryable is True
