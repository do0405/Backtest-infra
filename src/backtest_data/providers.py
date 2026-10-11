"""Read-only authenticated provider APIs with secrets confined to request memory."""
from datetime import date,timedelta
from decimal import Decimal
import json
import re
from zoneinfo import ZoneInfo

from .db import utcnow
from .ingestion.http import ProviderError
from .ingestion.krx_contract import KRX_ENDPOINTS


def _json(response, source):
    try:
        return json.loads(response.content)
    except (ValueError, UnicodeError):
        raise ProviderError(source, "invalid_json") from None


class KRXData:
    """Approved production reads; schema/units/history qualification is separate."""
    base="https://data-dbg.krx.co.kr"
    paths=KRX_ENDPOINTS

    def __init__(self,http,credentials):
        self.http,self.credentials=http,credentials

    def daily_quotes(self,service,session_date):
        completed_day=utcnow().astimezone(ZoneInfo('Asia/Seoul')).date()-timedelta(days=1)
        if service not in self.paths or type(session_date) is not date or not date(2010,1,4)<=session_date<=completed_day:
            raise ValueError('Registered KRX service and completed Korean date required')
        if not self.credentials.krx_api_key:
            raise ProviderError('krx','requires_credentials')
        response=self.http.request('krx','GET',self.base+self.paths[service],
            params={'basDd':session_date.strftime('%Y%m%d')},headers={'AUTH_KEY':self.credentials.krx_api_key.reveal()})
        try:
            def reject_constant(value):
                raise ValueError('nonfinite JSON constant')
            payload=json.loads(response.content,parse_float=Decimal,parse_constant=reject_constant)
            if not isinstance(payload,dict):
                raise ValueError('object response required')
        except (ValueError,UnicodeError):
            raise ProviderError('krx','invalid_json') from None
        return response,payload


class AlpacaData:
    """Free historical SIP reads using Trading API header credentials."""

    base = "https://data.alpaca.markets"

    def __init__(self, http, credentials):
        self.http, self.credentials = http, credentials

    def daily_prices(self, request):
        if not self.credentials.alpaca_key or not self.credentials.alpaca_secret:
            raise ProviderError("alpaca", "requires_credentials")
        if request.effective_end < request.start:
            raise ProviderError("alpaca", "free_history_range_deferred", retryable=True)
        response = self.http.request("alpaca", "GET", self.base + request.endpoint(), params=request.params(),
            headers={"APCA-API-KEY-ID": self.credentials.alpaca_key.reveal(),
                     "APCA-API-SECRET-KEY": self.credentials.alpaca_secret.reveal()})
        try:
            def reject_constant(value):
                raise ValueError("nonfinite JSON constant")
            payload = json.loads(response.content, parse_float=Decimal, parse_constant=reject_constant)
            if not isinstance(payload, dict):
                raise ValueError("object response required")
        except (ValueError, UnicodeError):
            raise ProviderError("alpaca", "invalid_json") from None
        return response, payload


class KISQuotes:
    base = "https://openapi.koreainvestment.com:9443"

    def __init__(self, http, credentials):
        self.http, self.credentials = http, credentials
        self._token, self._expires_at = None, None

    def _headers(self, tr_id="FHKST03010100"):
        if not self.credentials.kis_app_key or not self.credentials.kis_app_secret:
            raise ProviderError("kis", "requires_credentials")
        if not self._token or self._expires_at <= utcnow() + timedelta(seconds=30):
            response = self.http.request("kis", "POST", self.base + "/oauth2/tokenP", json={
                "grant_type": "client_credentials", "appkey": self.credentials.kis_app_key.reveal(),
                "appsecret": self.credentials.kis_app_secret.reveal()})
            payload = _json(response, "kis")
            if not payload.get("access_token"):
                raise ProviderError("kis", "token_unavailable")
            self._token = payload["access_token"]
            self._expires_at = utcnow() + timedelta(seconds=int(payload.get("expires_in", 3600)))
        return {"authorization": "Bearer " + self._token, "appkey": self.credentials.kis_app_key.reveal(),
                "appsecret": self.credentials.kis_app_secret.reveal(), "tr_id": tr_id, "custtype": "P"}

    def daily_prices(self, request):
        response = self.http.request("kis", "GET", self.base + "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                                     params=request.params(), headers=self._headers())
        payload = _json(response, "kis")
        if str(payload.get("rt_cd")) != "0":
            code = str(payload.get("msg_cd", "api_error"))
            if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", code):
                code = "api_error"
            raise ProviderError("kis", code, retryable=code in {"EGW00201", "EGW00133"})
        return response, payload

    def overseas_daily_prices(self, request):
        from .prices.adapters.kis_overseas import TR_ID
        response = self.http.request('kis','GET',self.base+request.endpoint(),
            params=request.params(),headers=self._headers(TR_ID))
        try:
            payload = json.loads(response.content,parse_float=Decimal)
            if not isinstance(payload,dict):
                raise ValueError('object response required')
        except (ValueError,UnicodeError):
            raise ProviderError('kis','invalid_json') from None
        if str(payload.get('rt_cd'))!='0':
            code = str(payload.get('msg_cd','api_error'))
            if not re.fullmatch(r'[A-Za-z0-9_]{1,32}',code):
                code='api_error'
            raise ProviderError('kis',code,retryable=code in {'EGW00201','EGW00133'})
        return response,payload


class OpenDART:
    base = "https://opendart.fss.or.kr/api/"

    def __init__(self, http, credentials):
        self.http, self.credentials = http, credentials

    def fetch(self, endpoint, params=None, *, json_response=True):
        if endpoint not in {"corpCode.xml", "list.json", "company.json", "document.xml", "fnlttXbrl.xml", "fnlttSinglAcntAll.json"}:
            raise ValueError("Unregistered OpenDART read endpoint")
        if not self.credentials.opendart_api_key:
            raise ProviderError("opendart", "requires_credentials")
        request_params = {**(params or {}), "crtfc_key": self.credentials.opendart_api_key.reveal()}
        response = self.http.request("opendart", "GET", self.base + endpoint, params=request_params, max_bytes=128 * 1024 * 1024)
        if not json_response:
            if endpoint in {"corpCode.xml", "fnlttXbrl.xml", "document.xml"} and not response.content.startswith(b"PK"):
                code_match = re.search(rb"<status>\s*(\d{3})\s*</status>", response.content[:8192])
                if code_match:
                    code = code_match.group(1).decode("ascii")
                    raise ProviderError("opendart", code, retryable=code in {"020", "800", "900"})
                raise ProviderError("opendart", "expected_zip_unavailable")
            return response
        payload = _json(response, "opendart")
        code = str(payload.get("status", "invalid_response"))
        if code != "000":
            if not re.fullmatch(r"\d{3}", code):
                code = "invalid_response"
            raise ProviderError("opendart", code, retryable=code in {"020", "800", "900"})
        return response, payload


class SEC:
    def __init__(self, http, credentials):
        self.http, self.credentials = http, credentials

    def fetch(self, url):
        if "@" not in self.credentials.sec_user_agent:
            raise ProviderError("sec", "requires_identifying_user_agent")
        return self.http.request("sec", "GET", url, headers={"User-Agent": self.credentials.sec_user_agent}, max_bytes=128 * 1024 * 1024)
