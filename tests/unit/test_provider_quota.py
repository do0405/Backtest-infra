from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backtest_data.ingestion.http import ProviderHTTP, ProviderResponse, ProviderError
from backtest_data.supplemental.transport import INTERVALS


def test_alpaca_shared_transport_defaults_keep_ten_percent_margin():
    transport = ProviderHTTP(None)
    try:
        assert transport.intervals['alpaca'] == 0.334
        assert INTERVALS['alpaca'] == 0.334
    finally:
        transport.close()


@pytest.mark.parametrize('json_response,content', [(True, b'{"status":"020"}'),
    (False, b'<result><status>020</status></result>')])
def test_dart_account_limit_persists_cooldown_before_returning_error(json_response, content):
    from backtest_data.providers import OpenDART
    class HTTP:
        def __init__(self):
            self.cooldowns = []
        def request(self, *args, **kwargs):
            return ProviderResponse(content, 200, datetime.now(timezone.utc), '')
        def cooldown(self, source, until):
            self.cooldowns.append((source, until))
    http = HTTP()
    api = OpenDART(http, SimpleNamespace(opendart_api_key=SimpleNamespace(reveal=lambda: 'dummy')))
    before = datetime.now(timezone.utc)
    with pytest.raises(ProviderError) as caught:
        api.fetch('list.json' if json_response else 'document.xml', json_response=json_response)
    assert caught.value.code == '020'
    assert caught.value.retryable
    assert len(http.cooldowns) == 1
    assert http.cooldowns[0][0] == 'opendart'
    assert http.cooldowns[0][1] >= before + timedelta(hours=24)


def test_portable_rate_state_rejects_foreign_binding_and_source():
    from backtest_data.ingestion.quota import validate_rate_state
    state = {'version': 1, 'binding': {'stream': 'owned'}, 'sources': ['krx'],
        'policies': [], 'rate_budgets': [], 'reservations': [], 'allocation_days': []}
    assert validate_rate_state(state, sources=['krx'], binding={'stream': 'owned'}) == state
    with pytest.raises(ValueError, match='binding'):
        validate_rate_state(state, sources=['krx'], binding={'stream': 'other'})
    state['rate_budgets'] = [{'key': 'opendart:shared_account',
        'next_available_at': datetime.now(timezone.utc).isoformat()}]
    with pytest.raises(ValueError, match='owned'):
        validate_rate_state(state, sources=['krx'], binding={'stream': 'owned'})


def test_portable_rate_state_requires_absolute_aware_clocks():
    from backtest_data.ingestion.quota import validate_rate_state
    state = {'version': 1, 'binding': {}, 'sources': ['krx'], 'policies': [],
        'rate_budgets': [{'key': 'krx:shared_account', 'next_available_at': '2026-10-12T12:00:00'}],
        'reservations': [], 'allocation_days': []}
    with pytest.raises(ValueError, match='aware'):
        validate_rate_state(state, sources=['krx'], binding={})
