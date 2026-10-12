"""Bounded read-only provider transport; error text never includes request secrets."""
from dataclasses import dataclass, field
from datetime import timedelta
from email.utils import parsedate_to_datetime
import random
import re
import time
import math
from urllib.parse import unquote, urlsplit

import httpx

from ..db import transaction, utcnow
from .jobs import reserve_rate_slot, RateReservationDeferred
from .quota import set_provider_cooldown
from .krx_contract import KRX_ENDPOINTS


class ProviderError(RuntimeError):
    def __init__(self, source, code, *, retryable=False):
        self.source, self.code, self.retryable = source, str(code), retryable
        super().__init__(f"{source}: {code}")


@dataclass(frozen=True)
class ProviderResponse:
    content: bytes
    status_code: int
    captured_at: object
    content_type: str


@dataclass
class HTTPAttemptBudget:
    """A single collector's admission limit, including transport retries."""
    deadline: float
    max_attempts: int | None = None
    started: int = field(default=0, init=False)

    def __post_init__(self):
        if not math.isfinite(self.deadline) or self.max_attempts is not None and (
            type(self.max_attempts) is not int or self.max_attempts <= 0):
            raise ValueError('Finite HTTP deadline and positive attempt limit required')

    def check(self,source,*,delay=0):
        if time.monotonic()+delay>=self.deadline or self.max_attempts is not None and self.started>=self.max_attempts:
            raise ProviderError(source,'rate_budget_deferred',retryable=True)

    def begin(self,source):
        self.check(source)
        self.started+=1


class ProviderHTTP:
    def __init__(self, engine, *, client=None, minimum_intervals=None, budget_scope="shared_account"):
        self.engine = engine
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.intervals = minimum_intervals or {"sec": 0.25, "opendart": 0.5, "kis": 0.5, "directory": 0.2, "alpaca": 0.334, "krx": 10.0}
        self.budget_scope = budget_scope
        self.request_budget = None

    def _validate(self, source, method, url):
        allowed = {"sec": {"www.sec.gov", "data.sec.gov"}, "opendart": {"opendart.fss.or.kr"},
                   "kis": {"openapi.koreainvestment.com", "openapivts.koreainvestment.com"},
                   "alpaca": {"data.alpaca.markets"},
                   "krx": {"data-dbg.krx.co.kr"},
                   "directory": {"www.nasdaqtrader.com", "new.real.download.dws.co.kr"}}
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in allowed.get(source, set()) or parts.username or parts.password:
            raise ValueError("Provider URL is outside source allowlist")
        if source == "alpaca":
            if method != "GET" or parts.port not in (None, 443) or parts.query or parts.fragment or not re.fullmatch(
                r"/v2/stocks/[A-Z0-9][A-Z0-9.\-^$]{0,119}/bars", unquote(parts.path)):
                raise ValueError("Alpaca transport permits only read-only daily stock bars")
        elif source == "krx":
            if method != "GET" or parts.port not in (None,443) or parts.query or parts.fragment or parts.path not in KRX_ENDPOINTS.values():
                raise ValueError("KRX transport permits only registered read-only market data")
        elif source == "kis":
            if not (method == "POST" and parts.path == "/oauth2/tokenP") and not (
                method == "GET" and parts.path.startswith(("/uapi/domestic-stock/v1/quotations/", "/uapi/overseas-price/"))):
                raise ValueError("Provider transport permits read-only quotes and token creation")
        elif method != "GET":
            raise ValueError("Provider transport is read-only")

    def request(self, source, method, url, *, params=None, headers=None, json=None, max_bytes=32 * 1024 * 1024):
        self._validate(source, method, url)
        if source not in self.intervals:
            raise ValueError("Unregistered source rate budget")
        for attempt in range(3):
            if self.request_budget is not None:self.request_budget.check(source)
            try:
                with transaction(self.engine) as session:
                    slot = reserve_rate_slot(session, f"{source}:{self.budget_scope}", utcnow(), self.intervals[source], max_delay_seconds=30)
            except RateReservationDeferred:
                raise ProviderError(source, "rate_budget_deferred", retryable=True) from None
            delay = (slot - utcnow()).total_seconds()
            if delay > 30:
                raise ProviderError(source, "rate_budget_deferred", retryable=True)
            # A charged reservation is never refunded if its wait outlives this
            # invocation. No HTTP begins after either its deadline or cap.
            if self.request_budget is not None:self.request_budget.check(source,delay=max(0,delay))
            if delay > 0:
                time.sleep(delay)
            try:
                if self.request_budget is not None:self.request_budget.begin(source)
                with self.client.stream(method, url, params=params, headers=headers, json=json) as response:
                    if response.status_code in {429, 500, 502, 503, 504}:
                        retry_delay = 0.0
                        if after := response.headers.get("Retry-After"):
                            try:
                                retry_delay = max(0.0, float(after))
                            except ValueError:
                                try:
                                    retry_delay = max(0.0, (parsedate_to_datetime(after) - utcnow()).total_seconds())
                                except (ValueError, TypeError):
                                    pass
                        if retry_delay:
                            self.cooldown(source, utcnow() + timedelta(seconds=retry_delay))
                        if retry_delay > 30:
                            raise ProviderError(source, "retry_deferred", retryable=True)
                        if attempt < 2:
                            time.sleep(max(retry_delay, min(2 ** attempt, 8)) + random.uniform(0, 0.25))
                            continue
                        raise ProviderError(source, response.status_code, retryable=True)
                    if response.status_code >= 300:
                        raise ProviderError(source, response.status_code)
                    pieces, count = [], 0
                    for piece in response.iter_bytes():
                        count += len(piece)
                        if count > max_bytes:
                            raise ProviderError(source, "response_size_exceeded")
                        pieces.append(piece)
                    return ProviderResponse(b"".join(pieces), response.status_code, utcnow(), response.headers.get("content-type", ""))
            except httpx.RequestError:
                if attempt == 2:
                    raise ProviderError(source, "transport_failed", retryable=True) from None
                time.sleep(2 ** attempt)

    def cooldown(self, source, until):
        if source not in self.intervals:
            raise ValueError('Unregistered source rate budget')
        with transaction(self.engine) as session:
            set_provider_cooldown(session, f'{source}:{self.budget_scope}', until)

    def close(self):
        self.client.close()
