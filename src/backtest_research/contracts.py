"""Exact input contracts shared by SQL projection, calculations and the local UI."""
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import re


def canonical_value(value):
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('finite decimal required')
        return {'$decimal': str(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('aware clock required')
        return {'$datetime': value.astimezone(timezone.utc).isoformat()}
    if isinstance(value, date):
        return {'$date': value.isoformat()}
    if hasattr(value, '__dataclass_fields__'):
        return canonical_value(asdict(value))
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError('string mapping keys required')
        return {key: canonical_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [canonical_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('finite value required')
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError('unsupported canonical input type')


def canonical_hash(value):
    body = json.dumps(canonical_value(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('aware clock required')
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class SeriesIdentity:
    security_id: str
    source_contract_id: str
    market: str
    source: str
    currency: str
    venue: str
    feed: str
    trade_scope: str
    basis: str
    adjustments: tuple[str, ...]
    volume_basis: str
    quantity_unit: str

    def __post_init__(self):
        if not self.security_id or not self.source_contract_id or self.market not in ('KR', 'US'):
            raise ValueError('security, source contract and market required')
        if self.basis not in ('as_traded', 'provider_adjusted'):
            raise ValueError('unsupported basis')
        if not isinstance(self.adjustments, tuple) or (self.basis == 'as_traded' and self.adjustments):
            raise ValueError('explicit consistent adjustment definition required')
        if self.basis == 'provider_adjusted' and not self.adjustments:
            raise ValueError('adjustment definition required')
        if not self.currency or not self.quantity_unit or not self.volume_basis:
            raise ValueError('explicit currency and quantity basis required')

    @property
    def key(self):
        return canonical_hash(self)


@dataclass(frozen=True)
class Bar:
    observation_id: str
    series: SeriesIdentity
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    captured_at: datetime
    available_at: datetime
    input_hash: str
    notional: Decimal | None = None
    raw_id: str | None = None
    decision_id: str | None = None

    def __post_init__(self):
        if type(self.session) is not date or not self.observation_id:
            raise ValueError('dated observation required')
        values = (self.open, self.high, self.low, self.close)
        if any(not isinstance(v, Decimal) or not v.is_finite() for v in values):
            raise ValueError('finite exact Decimal OHLC required')
        if min(values) <= 0 or self.low > min(self.open, self.close) or self.high < max(self.open, self.close) or self.low > self.high:
            raise ValueError('invalid OHLC structure')
        for value in (self.volume, self.notional):
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or value < 0):
                raise ValueError('finite nonnegative exact volume/notional required')
        aware(self.captured_at)
        aware(self.available_at)
        if not re.fullmatch('[0-9a-f]{64}', self.input_hash):
            raise ValueError('observation input hash required')


@dataclass(frozen=True)
class AnalysisDataset:
    dataset_id: str
    content_hash: str
    manifest_id: str
    manifest_hash: str
    original_target_count: int
    selected_security_ids: tuple[str, ...]
    bars: tuple[Bar, ...]
    sessions: tuple[date, ...]
    cutoff: datetime
    source_snapshot_at: datetime
    quality_gaps: tuple[str, ...]
    origin: str
    calendar_grade: str = 'observed_session_union'
    policy_version: str = 'bounded_actual_source_snapshot_v1'
    release_id: str | None = None
    calendar_evidence_refs: tuple[str, ...] = ()

    @classmethod
    def create(cls, *, bars, sessions, cutoff, manifest_id, manifest_hash, original_target_count,
               source_snapshot_at, quality_gaps, origin, calendar_grade='observed_session_union',
               policy_version='bounded_actual_source_snapshot_v1', release_id=None,calendar_evidence_refs=()):
        cutoff, source_snapshot_at = aware(cutoff), aware(source_snapshot_at)
        if origin not in ('actual_source', 'test_fixture'):
            raise ValueError('explicit actual source or fixture origin required')
        if origin == 'actual_source' and original_target_count != 17758:
            raise ValueError('original full target count must be retained')
        if not manifest_id or not re.fullmatch('[0-9a-f]{64}', manifest_hash):
            raise ValueError('original target manifest identity required')
        bars = tuple(sorted(bars, key=lambda b: (b.series.key, b.session, b.observation_id)))
        sessions = tuple(sessions)
        if len(bars) > 100000 or len(sessions) > 6000 or not sessions:
            raise ValueError('bounded nonempty session grid required')
        if sessions != tuple(sorted(set(sessions))) or any(type(d) is not date for d in sessions):
            raise ValueError('ordered unique sessions required')
        grid = set(sessions)
        keys = set()
        for bar in bars:
            if bar.session not in grid:
                raise ValueError('bar outside session grid')
            if bar.available_at > cutoff:
                raise ValueError('future bar beyond completed-day cutoff')
            key = (bar.series.key, bar.session)
            if key in keys:
                raise ValueError('duplicate series/session must be selected explicitly')
            keys.add(key)
        selected = tuple(sorted({b.series.security_id for b in bars}))
        if len(selected) > original_target_count:
            raise ValueError('selected count exceeds original target')
        payload = dict(manifest_id=manifest_id, manifest_hash=manifest_hash,
                       original_target_count=original_target_count, selected_security_ids=selected,
                       bars=bars, sessions=sessions, cutoff=cutoff, source_snapshot_at=source_snapshot_at,
                       quality_gaps=tuple(sorted(set(quality_gaps))), origin=origin, calendar_grade=calendar_grade,
                       policy_version=policy_version, release_id=release_id,calendar_evidence_refs=tuple(calendar_evidence_refs))
        digest = canonical_hash(payload)
        return cls(dataset_id=digest, content_hash=digest, **payload)

    @property
    def series(self):
        return tuple({b.series.key: b.series for b in self.bars}.values())

    def series_bars(self, series_key):
        return tuple(b for b in self.bars if b.series.key == series_key)
