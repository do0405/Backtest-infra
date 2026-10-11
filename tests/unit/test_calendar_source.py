from datetime import date
import pytest
from backtest_data.coverage.calendar_source import CalendarHTTP, decode_calendar


def test_calendar_request_dates_are_serializable():
    from backtest_data.coverage.calendar_source import calendar_request
    from backtest_data.ingestion.jobs import stable_hash
    params=calendar_request(date(2016,1,1),date(2026,10,7))
    assert params=={'start':'2016-01-01','end':'2026-10-07','date_type':'TRADING'}
    assert len(stable_hash(params))==64


def test_calendar_transport_cannot_order_or_follow_untrusted_host():
    transport = object.__new__(CalendarHTTP)
    transport._validate('alpaca', 'GET', 'https://paper-api.alpaca.markets/v2/calendar')
    for method, url in [('POST', 'https://paper-api.alpaca.markets/v2/calendar'),
        ('GET', 'https://paper-api.alpaca.markets/v2/orders'),
        ('GET', 'https://paper-api.alpaca.markets.evil.example/v2/calendar'),
        ('GET', 'https://paper-api.alpaca.markets/v2/calendar?secret=x')]:
        with pytest.raises(ValueError):
            transport._validate('alpaca', method, url)


def test_calendar_preserves_early_close_and_does_not_certify_exchange():
    result = decode_calendar(b'[{"date":"2026-07-02","open":"09:30","close":"13:00"}]',
        start=date(2026,7,1), end=date(2026,7,4))
    assert result[0]['close'] == '13:00'
    assert result[0]['status'] == 'open'


@pytest.mark.parametrize('body', [b'[]', b'{"error":"secret"}',
    b'[{"date":"2026-07-02","open":"16:00","close":"09:30"}]',
    b'[{"date":"2026-07-02","open":"09:30","close":"16:00"},{"date":"2026-07-02","open":"09:30","close":"16:00"}]'])
def test_invalid_calendar_never_proves_closure(body):
    with pytest.raises(ValueError):
        decode_calendar(body, start=date(2026,7,1), end=date(2026,7,4))
