import httpx
import pytest

from backtest_data.ingestion.http import ProviderHTTP, ProviderError
from backtest_data.db import new_id


pytestmark = pytest.mark.integration


def test_provider_transport_rejects_credential_forwarding_to_unknown_hosts():
    transport = ProviderHTTP(None)
    with pytest.raises(ValueError, match="allowlist"):
        transport.request("opendart", "GET", "https://example.invalid/api", params={"crtfc_key": "dummy"})


def test_provider_transport_disallows_broker_order_calls():
    transport = ProviderHTTP(None)
    with pytest.raises(ValueError, match="read-only"):
        transport.request("kis", "POST", "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/trading/order-cash")


def test_provider_errors_never_render_credential_query_string(pg_engine):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(401)))
    transport = ProviderHTTP(pg_engine, client=client, budget_scope=new_id())
    with pytest.raises(ProviderError) as caught:
        transport.request("opendart", "GET", "https://opendart.fss.or.kr/api/list.json", params={"crtfc_key": "dummy-secret"})
    assert "dummy-secret" not in str(caught.value)
    assert "crtfc_key" not in str(caught.value)


def test_retry_after_long_delay_is_deferred_instead_of_ignored(pg_engine):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": "120"})))
    transport = ProviderHTTP(pg_engine, client=client, budget_scope=new_id())
    with pytest.raises(ProviderError, match="retry_deferred"):
        transport.request("sec", "GET", "https://data.sec.gov/submissions/CIK0000000001.json")
def test_deferred_requests_do_not_consume_phantom_source_slots(db_session, monkeypatch):
    from datetime import timedelta
    from contextlib import contextmanager
    import backtest_data.ingestion.http as module
    from backtest_data.ingestion.models import RateBudget
    from backtest_data.db import utcnow
    now = utcnow()
    budget = RateBudget(key="sec:phantom-test", next_available_at=now + timedelta(seconds=120))
    db_session.add(budget)
    db_session.flush()
    @contextmanager
    def tx(engine):
        yield db_session
    monkeypatch.setattr(module, "transaction", tx)
    http = module.ProviderHTTP(None, budget_scope="phantom-test")
    try:
        for _ in range(5):
            with pytest.raises(module.ProviderError) as caught:
                http.request("sec", "GET", "https://data.sec.gov/submissions/a.json")
            assert caught.value.code == "rate_budget_deferred"
        db_session.refresh(budget)
        assert budget.next_available_at == now + timedelta(seconds=120)
    finally:
        http.close()
