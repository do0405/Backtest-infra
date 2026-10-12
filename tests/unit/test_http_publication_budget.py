from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as N

import httpx
import pytest

from backtest_data.ingestion import http as module


@pytest.mark.parametrize('kind', ['retry_deadline', 'retry_count', 'rate_wait'])
def test_every_http_attempt_obeys_publication_budget_including_retry_and_wait(monkeypatch, kind):
    clock = [0.0]; calls = []; reservations = []
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    @contextmanager
    def transaction(_):
        yield N()
    def reserve(*args, **kwargs):
        reservations.append(1)
        return now + timedelta(seconds=1 if kind == 'rate_wait' else clock[0])
    def handle(request):
        calls.append(1)
        return httpx.Response(503)
    monkeypatch.setattr(module, 'transaction', transaction)
    monkeypatch.setattr(module, 'reserve_rate_slot', reserve)
    monkeypatch.setattr(module, 'utcnow', lambda: now + timedelta(seconds=clock[0]))
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(module.random, 'uniform', lambda *args: 0)
    client = httpx.Client(transport=httpx.MockTransport(handle))
    transport = module.ProviderHTTP(None, client=client)
    transport.request_budget = module.HTTPAttemptBudget(deadline=0.5 if kind != 'retry_count' else 100,
                                                        max_attempts=1 if kind == 'retry_count' else 256)
    with pytest.raises(module.ProviderError, match='rate_budget_deferred'):
        transport.request('sec', 'GET', 'https://www.sec.gov/test')
    assert len(calls) == transport.request_budget.started == (0 if kind == 'rate_wait' else 1)
    assert len(reservations) == 1
    transport.close()
