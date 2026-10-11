"""Matched historical REST-trade statistics under an explicit daily rule.

The rule is the current Alpaca documented daily high/low AND volume eligibility
table, pinned as v1. It is not a certificate of historical rule versions or of
the SIP's complete cancellation stream. Both sums use exactly the same returned
trade population. Ordinary bar volume is never used as the denominator.
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from ..ingestion.jobs import stable_hash
from ..supplemental.alpaca_inputs import _timestamp, _boundary
from .numeric import exact_multiply

NY = ZoneInfo('America/New_York')
RULE = 'alpaca_daily_high_low_and_volume_v1'
IDENTITY_POLICY = 'tape_venue_id_nanosecond_v2'
INPUT_POLICY = 'eligible_positive_excluded_nonnegative_v2'
REFERENCE = 'https://docs.alpaca.markets/us/docs/market-data-faq'
# Documented daily rules: green high/low AND green volume. Multiple conditions
# combine by AND. Tape-specific exclusions cannot be replaced by one global set.
ELIGIBLE = {
    'A': frozenset(' EFKLOPXZ456'),
    'B': frozenset(' EFKLOPXZ456'),
    'C': frozenset('@ABDFGKLOPXYZ456'),
}
VALID = {
    'A': frozenset(' BCEFHIKLMNOPQRTUVXZ45679'),
    'B': frozenset(' BCEFHIKLMNOPQRTUVXZ45679'),
    'C': frozenset('@ABCDFGHIKLMNOPQRTUVWXYZ45679'),
}


def eligible_daily_trade(tape, conditions):
    if tape not in VALID:
        raise ValueError('unsupported_trade_tape')
    if (not isinstance(conditions, (list, tuple)) or not conditions
            or any(not isinstance(c, str) or c not in VALID[tape] for c in conditions)):
        raise ValueError('unknown_trade_condition_for_tape')
    return all(c in ELIGIBLE[tape] for c in conditions)


def trade_day_bounds(session):
    if type(session) is not date:
        raise ValueError('exact_session_date_required')
    start = datetime.combine(session, time(), NY)
    return start, datetime.combine(session + timedelta(days=1), time(), NY)


def trade_day_request(symbol, session, *, token=None, limit=10000):
    import re
    if not isinstance(symbol, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.\-^$]{0,119}', symbol):
        raise ValueError('literal_source_symbol_required')
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError('bounded_trade_page_required')
    start, end = trade_day_bounds(session)
    params = dict(start=start.isoformat(), end=end.isoformat(), feed='sip', sort='asc',
                  limit=limit, asof='-')
    if token is not None:
        if not isinstance(token, str) or not token:
            raise ValueError('valid_page_token_required')
        params['page_token'] = token
    return params


def source_trade_symbol(member):
    """Use the exact CQS alias on the same official directory row; no rewrite."""
    symbol=member.get('symbol');evidence=member.get('evidence',member.get('fields',{}))
    if member.get('market')!='US' or member.get('identifier')!='nasdaq_directory:'+str(symbol):
        raise ValueError('official_directory_symbol_binding_required')
    if 'ACT Symbol' in evidence:
        if evidence['ACT Symbol']!=symbol:raise ValueError('conflicting_directory_symbol_binding')
        provider=evidence.get('CQS Symbol')
        if isinstance(provider,str) and provider:
            trade_day_request(provider,date(2026,10,7))
            return provider
    trade_day_request(symbol,date(2026,10,7))
    return symbol


def _number(value, *, allow_zero=False):
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError('exact_trade_number_required')
    result = Decimal(value)
    if not result.is_finite() or result < 0 or result == 0 and not allow_zero:
        raise ValueError('positive_trade_number_required')
    exact_multiply(result, Decimal(1))  # numeric envelope guard
    return result


def normalize_trade_page(payload, *, symbol, session):
    issues, events = [], []
    if not isinstance(payload, dict) or payload.get('symbol') != symbol:
        issues.append('response_symbol_mismatch')
    if not isinstance(payload, dict) or not isinstance(payload.get('trades'), list):
        raise ValueError('explicit_trade_array_required')
    if 'next_page_token' not in payload or (payload['next_page_token'] is not None
            and (not isinstance(payload['next_page_token'], str) or not payload['next_page_token'])):
        issues.append('pagination_metadata_invalid')
    start, end = map(_boundary, trade_day_bounds(session))
    previous = None
    for row in payload['trades']:
        if not isinstance(row,dict):
            issues.append('invalid_trade_object')
            continue
        try:
            eligible = eligible_daily_trade(row.get('z'), row.get('c'))
            identity = [str(row[k]) for k in ('z', 'x', 'i')]
            if any(row.get(k) is None or isinstance(row.get(k), bool) or not str(row[k]) for k in ('z', 'x', 'i')):
                raise ValueError('unresolved_trade_identity')
            stamp = _timestamp(row['t'])
            price, quantity = _number(row['p'],allow_zero=not eligible), _number(row['s'],allow_zero=not eligible)
            if previous is not None and stamp < previous:
                issues.append('nonmonotonic_trade_page')
            previous = stamp
            # REST end can be inclusive; next midnight belongs to the next day.
            if not start <= stamp < end:
                continue
            fingerprint = stable_hash([identity,stamp[0].isoformat(),stamp[1],str(price),str(quantity),row['c']])
            events.append(dict(event_key=stable_hash([identity,stamp[0].isoformat(),stamp[1]]), fingerprint=fingerprint,
                timestamp_second=stamp[0], timestamp_nanosecond=stamp[1],
                price=price, quantity=quantity, numerator=exact_multiply(price, quantity),
                eligible=eligible))
        except (ValueError, KeyError, TypeError, ArithmeticError):
            issues.append('invalid_or_unknown_trade')
    return events, list(dict.fromkeys(issues))
