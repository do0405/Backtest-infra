"""Small exact fixtures and hand ledger values; never a production engine."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from backtest_research.contracts import AnalysisDataset, Bar, SeriesIdentity


def dataset(prices, *, opens=None, missing=(), volumes=None, security='A', market='US', basis='as_traded', adjustments=()):
    clock = datetime(2025, 1, 1, tzinfo=timezone.utc)
    identity = SeriesIdentity(security, 'test-contract', market, 'fixture', 'USD' if market == 'US' else 'KRW',
                              'XNYS' if market == 'US' else 'XKRX', 'fixture', 'daily', basis, adjustments, 'as_traded', 'shares')
    # Weekday grid is a declared fixture calendar, not a market closure claim.
    sessions = []
    day = date(2020, 1, 2)
    while len(sessions) < len(prices):
        if day.weekday() < 5:
            sessions.append(day)
        day += timedelta(days=1)
    bars = []
    for i, (session, price) in enumerate(zip(sessions, prices)):
        if i in missing:
            continue
        close, opening = Decimal(str(price)), Decimal(str(opens[i] if opens else price))
        volume = Decimal(str(volumes[i] if volumes else 1000))
        bars.append(Bar(str(i), identity, session, opening, max(opening, close), min(opening, close), close,
                        volume, clock, datetime.combine(session, datetime.min.time(), timezone.utc), 'a' * 64))
    return AnalysisDataset.create(bars=bars, sessions=sessions, cutoff=clock, manifest_id='fixture-target',
                                  manifest_hash='b' * 64, original_target_count=17758, source_snapshot_at=clock,
                                  quality_gaps=('corporate_action_coverage_unknown',), origin='test_fixture',
                                  calendar_grade='declared_test_sessions')


def hand_next_open_ledger():
    """SMA2 long after 10,12; buy 6 at next open 15; sell 6 at 8."""
    return {'buy_quantity': 6, 'buy_price': 15, 'buy_cash': 10,
            'sell_quantity': -6, 'sell_price': 8, 'final_cash': 58,
            'nav': [100, 100, 106, 58, 58]}
