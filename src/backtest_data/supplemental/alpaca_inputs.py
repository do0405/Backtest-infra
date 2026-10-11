"""Read-only Alpaca request plans and unadopted corporate-action/trade inputs.

Corporate-action search dates are process dates, never publication dates.
The regular-sale-only candidate is a deliberately narrower population than
Alpaca's official SIP VWAP. Complete pagination alone supplies neither verified
security identity, historical publication evidence, nor action-ledger coverage.
References: docs.alpaca.markets/us/reference/corporateactions-1,
stocktradesingle-1, and /us/docs/market-data-faq.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from ..prices.actions import CorporateAction
from ..prices.domain import PriceContext, require_aware
from ..prices.numeric import exact_multiply, exact_sum, numeric_signature
from ..prices.vwap import DailyVWAPStat


START_DATE = date(2016, 1, 1)
END_DATE = date(2026, 10, 7)
TRADE_SCOPE = "regular_sale_only_v1"
NEW_YORK = ZoneInfo("America/New_York")
KNOWN_CONDITIONS = frozenset({" ", "@", "A", "B", "C", "D", "E", "F", "G", "H", "I", "K", "L", "M",
                              "N", "O", "P", "Q", "R", "T", "U", "V", "W", "X", "Y", "Z", "4", "5", "6", "7", "9"})
SIMPLE_SPLITS = frozenset({"forward_splits", "reverse_splits"})
KNOWN_ACTION_TYPES = SIMPLE_SPLITS | frozenset({"unit_splits", "cash_dividends", "stock_dividends", "spin_offs",
    "cash_mergers", "stock_mergers", "stock_and_cash_mergers", "redemptions", "name_changes", "worthless_removals",
    "rights_distributions", "partial_calls", "reorganizations", "capital_gains_distributions"})
NUMERIC_TERMS = frozenset({"new_rate", "old_rate", "rate", "source_rate", "alternate_rate", "acquirer_rate",
                           "acquiree_rate", "cash_rate"})


def _decimal(value: Any, *, positive: bool = False) -> Decimal:
    if isinstance(value, (float, bool)):
        raise ValueError("binary float/bool is not an exact source number")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise ValueError("invalid exact source number") from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError("source number must be finite and positive/nonnegative")
    return result


def decode_alpaca_json(payload: str | bytes) -> Any:
    """Decode response bytes before any binary float can lose source digits."""
    def reject_constant(value):
        raise ValueError(f"nonfinite JSON number: {value}")
    return json.loads(payload, parse_float=Decimal, parse_int=Decimal, parse_constant=reject_constant)


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class AlpacaRequestSpec:
    method: str
    url: str
    params: tuple[tuple[str, str], ...]

    def __post_init__(self):
        if self.method != "GET" or not self.url.startswith("https://data.alpaca.markets/"):
            raise ValueError("only official read-only Alpaca data GET requests are allowed")
        params = tuple(tuple(pair) for pair in self.params)
        if any(len(pair) != 2 or not all(isinstance(v, str) for v in pair) for pair in params):
            raise ValueError("request parameter names and values must be explicit strings")
        object.__setattr__(self, "params", params)
        if len(dict(self.params)) != len(self.params):
            raise ValueError("request parameter names must be unique")

    @property
    def anchor_key(self) -> str:
        params = tuple(sorted((k, v) for k, v in self.params if k != "page_token"))
        return sha256(repr((self.method, self.url, params)).encode()).hexdigest()

    @property
    def request_key(self) -> str:
        return sha256(repr((self.method, self.url, tuple(sorted(self.params)))).encode()).hexdigest()

    def with_page_token(self, token: str | None) -> "AlpacaRequestSpec":
        params = tuple((k, v) for k, v in self.params if k != "page_token")
        if token is not None:
            if not isinstance(token, str) or not token:
                raise ValueError("page token must be a nonempty opaque string")
            params += (("page_token", token),)
        return replace(self, params=params)


def corporate_action_requests(start: date = START_DATE, end: date = END_DATE, *,
                              limit: int = 1000) -> tuple[AlpacaRequestSpec, ...]:
    """Inclusive yearly process-date windows; omitted symbols/types mean all."""
    if start < START_DATE or start > end or end > END_DATE:
        raise ValueError("corporate-action interval exceeds configured period/cutoff")
    if not 1 <= limit <= 1000:
        raise ValueError("corporate-action page limit must be 1..1000")
    plans = []
    for year in range(start.year, end.year + 1):
        lo, hi = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
        plans.append(AlpacaRequestSpec("GET", "https://data.alpaca.markets/v1/corporate-actions",
            (("start", lo.isoformat()), ("end", hi.isoformat()), ("limit", str(limit)),
             ("data_quality", "all"), ("region", "us"), ("sort", "asc"))))
    return tuple(plans)


def _validate_session(session: date, session_open: datetime, session_close: datetime):
    for name, value in (("session_open", session_open), ("session_close", session_close)):
        require_aware(value, name)
        if value is None or value.astimezone(NEW_YORK).date() != session:
            raise ValueError("calendar bounds must belong to the New York session date")
    if session_open >= session_close or not START_DATE <= session <= END_DATE:
        raise ValueError("session interval is reversed or exceeds cutoff")


def trade_request(symbol: str, session: date, *, session_open: datetime, session_close: datetime,
                  cutoff: datetime, limit: int = 10000) -> AlpacaRequestSpec:
    """Closed calendar interval with literal ticker identity and explicit SIP."""
    _validate_session(session, session_open, session_close)
    require_aware(cutoff, "cutoff")
    if cutoff is None or session_close > cutoff:
        raise ValueError("trade request exceeds closed-session cutoff")
    if not symbol or not 1 <= limit <= 10000:
        raise ValueError("symbol and page limit 1..10000 are required")
    return AlpacaRequestSpec("GET", f"https://data.alpaca.markets/v2/stocks/{quote(symbol, safe='')}/trades",
        (("start", session_open.isoformat()), ("end", session_close.isoformat()), ("limit", str(limit)),
         ("feed", "sip"), ("currency", "USD"), ("sort", "asc"), ("asof", "-")))


def _date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        raise ValueError("date field must retain source date precision")
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def normalize_corporate_action_page(payload: Mapping, *, cutoff: date = END_DATE) -> list[dict]:
    """Keep every source type and raw term; never infer publication or adoption."""
    if cutoff > END_DATE:
        raise ValueError("corporate-action cutoff exceeds configured period")
    buckets = payload.get("corporate_actions")
    if not isinstance(buckets, Mapping):
        raise ValueError("corporate_actions object is missing")
    rows = []
    identities: dict[str, list[dict]] = {}
    for raw_type, values in buckets.items():
        if not isinstance(values, (list, tuple)):
            raise ValueError("corporate-action type bucket must be an array")
        for raw in values:
            if not isinstance(raw, Mapping):
                raise ValueError("corporate-action record must be an object")
            fields = _thaw(_freeze(raw))
            for key in NUMERIC_TERMS & fields.keys():
                if fields[key] not in (None, ""):
                    fields[key] = _decimal(fields[key])
            issues = []
            ticker = next((str(raw[k]) for k in ("symbol", "old_symbol", "source_symbol", "acquiree_symbol") if raw.get(k)), "")
            event_id = str(raw.get("id") or "")
            if not ticker:
                issues.append("source_ticker_missing")
            if not event_id:
                issues.append("event_id_missing")
                event_id = "unresolved:" + numeric_signature(str(raw_type), _freeze(fields))
            effective, ex = _date(raw.get("effective_date")), _date(raw.get("ex_date"))
            record, payable = _date(raw.get("record_date")), _date(raw.get("payable_date"))
            economic = effective or ex
            if economic is None:
                issues.append("effective_date_missing")
            if raw_type not in KNOWN_ACTION_TYPES:
                issues.append("unsupported_action_type")
            process = _date(raw.get("process_date"))
            if process is None:
                issues.append("process_date_missing")
            elif process > cutoff:
                issues.append("process_date_after_cutoff")
            future = economic is not None and economic > cutoff
            if future:
                issues.append("future_effective")
            if raw_type in SIMPLE_SPLITS and not all(fields.get(k) is not None and fields[k] > 0 for k in ("new_rate", "old_rate")):
                issues.append("split_terms_missing")
            if raw_type == "cash_dividends" and fields.get("rate") is None:
                issues.append("cash_terms_missing")
            fields.update(raw_type=str(raw_type), future_effective=future, verification_status="pending")
            row = dict(record_kind="corporate_action", entity_key=ticker, row_key=event_id,
                effective_date=economic, ex_date=ex, record_date=record, pay_date=payable,
                currency=raw.get("currency"), fields=fields, issues=issues,
                date_basis="effective_date" if effective else "ex_date" if ex else "unknown")
            previous = identities.setdefault(event_id, [])
            if any(p == row for p in previous):
                continue
            if previous:
                for conflict in previous + [row]:
                    if "conflicting_action_identity" not in conflict["issues"]:
                        conflict["issues"].append("conflicting_action_identity")
            previous.append(row)
            rows.append(row)
    return rows


@dataclass(frozen=True, slots=True)
class CapturedTradePage:
    request: AlpacaRequestSpec
    payload: Mapping
    raw_ref: str
    captured_at: datetime

    def __post_init__(self):
        require_aware(self.captured_at, "captured_at")
        if not self.raw_ref or self.captured_at is None or not isinstance(self.payload, Mapping):
            raise ValueError("page payload, raw reference, and capture time must be explicit")
        object.__setattr__(self, "payload", _freeze(self.payload))


@dataclass(frozen=True, slots=True)
class CapturedCorporateActionPage:
    request: AlpacaRequestSpec
    payload: Mapping
    raw_ref: str
    captured_at: datetime

    def __post_init__(self):
        require_aware(self.captured_at, "captured_at")
        if not self.raw_ref or self.captured_at is None or not isinstance(self.payload, Mapping):
            raise ValueError("corporate-action page provenance must be explicit")
        object.__setattr__(self, "payload", _freeze(self.payload))


@dataclass(frozen=True, slots=True)
class CorporateActionPageChainStatus:
    status: str
    issues: tuple[str, ...]
    page_count: Decimal
    action_coverage_verified: bool = False


def corporate_action_page_chain_status(pages: Sequence[CapturedCorporateActionPage]) -> CorporateActionPageChainStatus:
    """Request exhaustion is never evidence of all-type economic coverage."""
    issues, seen_tokens = [], set()
    expected_token = None
    terminal = False
    for position, page in enumerate(pages):
        params = dict(page.request.params)
        try:
            start, end = _date(params.get("start")), _date(params.get("end"))
            plan = corporate_action_requests(start, end, limit=int(params.get("limit", "0")))
            valid_window = len(plan) == 1 and plan[0].anchor_key == page.request.anchor_key
        except (ValueError, TypeError):
            valid_window = False
        if (page.request.url != "https://data.alpaca.markets/v1/corporate-actions"
                or page.request.anchor_key != pages[0].request.anchor_key
                or not valid_window):
            issues.append("request_anchor_mismatch")
        token = params.get("page_token")
        if position == 0 and token is not None:
            issues.append("missing_initial_page")
        elif token != expected_token:
            issues.append("page_chain_mismatch")
        if position and expected_token is None:
            issues.append("page_after_terminal")
        if token is not None and token in seen_tokens:
            issues.append("page_token_cycle")
        seen_tokens.add(token)
        if not isinstance(page.payload.get("corporate_actions"), Mapping):
            issues.append("corporate_actions_missing")
        if "next_page_token" not in page.payload:
            issues.append("pagination_metadata_missing")
        expected_token = page.payload.get("next_page_token")
        if expected_token is not None and (not isinstance(expected_token, str) or not expected_token):
            issues.append("invalid_next_page_token")
        terminal = "next_page_token" in page.payload and expected_token is None
    if not pages or not terminal:
        issues.append("missing_corporate_action_pages")
    return CorporateActionPageChainStatus("pending" if issues else "complete", tuple(dict.fromkeys(issues)), Decimal(len(pages)))


def _timestamp(value: Any) -> tuple[datetime, int]:
    """Compare RFC3339 nanoseconds without Python's microsecond truncation."""
    if not isinstance(value, str):
        raise ValueError("trade timestamp must be RFC3339")
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)", value)
    if not match:
        raise ValueError("trade timestamp must be timezone-aware RFC3339")
    second = datetime.fromisoformat(match[1] + match[3].replace("Z", "+00:00")).astimezone(timezone.utc)
    return second, int((match[2] or "").ljust(9, "0") or "0")


def _boundary(value: datetime) -> tuple[datetime, int]:
    value = value.astimezone(timezone.utc)
    return value.replace(microsecond=0), value.microsecond * 1000


@dataclass(frozen=True, slots=True)
class TradeAggregateCandidate:
    symbol: str
    session: date
    session_open: datetime
    session_close: datetime
    cutoff: datetime
    sum_price_volume: Decimal
    eligible_volume: Decimal
    eligible_trade_count: Decimal
    excluded_trade_count: Decimal
    duplicate_count: Decimal
    page_count: Decimal
    page_chain_complete: bool
    issues: tuple[str, ...]
    input_event_ids: tuple[str, ...]
    raw_refs: tuple[str, ...]
    captured_at: datetime | None
    anchor_key: str
    signature: str
    trade_scope: str = TRADE_SCOPE

    @property
    def qualified(self) -> bool:
        """Only a complete exact candidate population, never full VWAP proof."""
        return self.page_chain_complete and not self.issues

    def as_staged_row(self) -> dict:
        return dict(record_kind="trade_aggregate", entity_key=self.symbol, row_key=self.signature,
            report_date=self.session, currency="USD", sum_price_volume=self.sum_price_volume,
            eligible_volume=self.eligible_volume, issues=list(self.issues), date_basis="new_york_session",
            fields=dict(trade_scope=self.trade_scope, status="candidate" if self.qualified else "unqualified",
                full_vwap_certified=False, historical_replay_supported=False,
                eligible_trade_count=self.eligible_trade_count, excluded_trade_count=self.excluded_trade_count,
                duplicate_count=self.duplicate_count, page_count=self.page_count,
                page_chain_complete=self.page_chain_complete, request_anchor=self.anchor_key,
                session_open=self.session_open.isoformat(), session_close=self.session_close.isoformat(),
                raw_refs=list(self.raw_refs), input_signature=self.signature))


def aggregate_trade_pages(pages: Sequence[CapturedTradePage], *, symbol: str, session: date,
                          session_open: datetime, session_close: datetime,
                          cutoff: datetime) -> TradeAggregateCandidate:
    """Aggregate the literal ['@'] subset using the same trades for N and D.

    Unknown conditions, conflicting identities, unclosed days, missing pages,
    or changed request parameters prevent qualification. Historical REST trades
    contain current corrected state; they do not certify replay of old events.
    """
    _validate_session(session, session_open, session_close)
    require_aware(cutoff, "cutoff")
    issues = []
    if cutoff is None:
        raise ValueError("cutoff must be explicit")
    if session_close > cutoff:
        issues.append("session_not_closed")
    # Building the expected anchor must not mistake an intraday cutoff for a
    # usable completed-day query; qualification is blocked above.
    expected = trade_request(symbol, session, session_open=session_open, session_close=session_close,
                             cutoff=max(cutoff, session_close))
    expected_token = None
    seen_tokens = set()
    seen: dict[tuple[str, str, str], tuple] = {}
    numerators, quantities, input_ids = [], [], []
    eligible = excluded = duplicates = Decimal(0)
    chain_complete = False
    previous_timestamp = None
    raw_refs = []
    for position, page in enumerate(pages):
        params = dict(page.request.params)
        # Page size is an immutable anchor parameter, but caller may choose a
        # bounded nondefault size. Validate it separately then build its anchor.
        try:
            anchored = trade_request(symbol, session, session_open=session_open, session_close=session_close,
                cutoff=max(cutoff, session_close), limit=int(params.get("limit", "0")))
        except ValueError:
            anchored = expected
            issues.append("request_anchor_mismatch")
        if page.request.anchor_key != anchored.anchor_key or (position and page.request.anchor_key != pages[0].request.anchor_key):
            issues.append("request_anchor_mismatch")
        actual_token = params.get("page_token")
        if position == 0 and actual_token is not None:
            issues.append("missing_initial_page")
        elif actual_token != expected_token:
            issues.append("page_chain_mismatch")
        if position and expected_token is None:
            issues.append("page_after_terminal")
        if actual_token is not None and actual_token in seen_tokens:
            issues.append("page_token_cycle")
        seen_tokens.add(actual_token)
        payload = page.payload
        if payload.get("symbol") != symbol:
            issues.append("response_symbol_mismatch")
        rows = payload.get("trades")
        if not isinstance(rows, (list, tuple)):
            raise ValueError("trade response must contain an array")
        if "next_page_token" not in payload:
            issues.append("pagination_metadata_missing")
        expected_token = payload.get("next_page_token")
        if expected_token is not None and (not isinstance(expected_token, str) or not expected_token):
            issues.append("invalid_next_page_token")
        chain_complete = "next_page_token" in payload and expected_token is None
        raw_refs.append(page.raw_ref)
        for raw in rows:
            if not isinstance(raw, Mapping):
                raise ValueError("trade row must be an object")
            conditions = raw.get("c")
            if not isinstance(conditions, (list, tuple)) or not conditions or any(c not in KNOWN_CONDITIONS for c in conditions):
                issues.append("unknown_trade_condition")
                excluded = exact_sum((excluded, Decimal(1)))
                continue
            identity = tuple(str(raw[k]) if raw.get(k) is not None and not isinstance(raw[k], bool) else "" for k in ("z", "x", "i"))
            if not all(identity) or identity[0] not in {"A", "B", "C"}:
                issues.append("unresolved_trade_identity")
                continue
            price, quantity = _decimal(raw.get("p"), positive=True), _decimal(raw.get("s"), positive=True)
            timestamp = _timestamp(raw.get("t"))
            fingerprint = (timestamp, price, quantity, tuple(conditions))
            if identity in seen:
                duplicates = exact_sum((duplicates, Decimal(1)))
                if seen[identity] != fingerprint:
                    issues.append("conflicting_trade_identity")
                continue
            seen[identity] = fingerprint
            if previous_timestamp is not None and timestamp < previous_timestamp:
                issues.append("nonmonotonic_trade_page")
            previous_timestamp = timestamp
            in_session = (timestamp[0].astimezone(NEW_YORK).date() == session
                          and _boundary(session_open) <= timestamp <= _boundary(session_close)
                          and timestamp <= _boundary(cutoff))
            if tuple(conditions) != ("@",) or not in_session:
                excluded = exact_sum((excluded, Decimal(1)))
                continue
            numerators.append(exact_multiply(price, quantity))
            quantities.append(quantity)
            input_ids.append(json.dumps(identity, separators=(",", ":")))
            eligible = exact_sum((eligible, Decimal(1)))
    if not pages or not chain_complete:
        issues.append("missing_trade_pages")
    issues = tuple(dict.fromkeys(issues))
    numerator, denominator = exact_sum(numerators), exact_sum(quantities)
    anchor_key = pages[0].request.anchor_key if pages else expected.anchor_key
    signature = numeric_signature(symbol, session, session_open, session_close, cutoff, anchor_key,
                                  tuple(sorted(seen.items())), tuple(raw_refs), numerator, denominator, issues)
    return TradeAggregateCandidate(symbol, session, session_open, session_close, cutoff,
        numerator, denominator, eligible, excluded, duplicates, Decimal(len(pages)), chain_complete,
        issues, tuple(input_ids), tuple(raw_refs), max((p.captured_at for p in pages), default=None),
        anchor_key, signature)


@dataclass(frozen=True, slots=True)
class ActionComparison:
    status: str
    reasons: tuple[str, ...]
    evidence_refs: tuple[str, ...] = ()


def compare_corporate_actions(left: Mapping, right: Mapping, *, left_source: str, right_source: str,
                              same_event_evidence: str | None = None,
                              independence_evidence: str | None = None) -> ActionComparison:
    """Two matching vendor rows never prove independent upstream provenance."""
    if not same_event_evidence or not independence_evidence or not left_source or left_source == right_source:
        return ActionComparison("pending", ("identity_or_source_independence_unproved",))
    a, b = left["fields"], right["fields"]
    evidence = (same_event_evidence, independence_evidence)
    # Different provider event IDs/process dates are expected. Everything else,
    # including future provider-specific terms, must agree or be adjudicated.
    non_economic = {"id", "process_date", "symbol", "old_symbol", "source_symbol", "acquiree_symbol",
                    "raw_type", "future_effective", "verification_status"}
    terms = (set(a) | set(b)) - non_economic
    differences = tuple(k for k in sorted(terms) if a.get(k) != b.get(k))
    if a.get("raw_type") != b.get("raw_type") or left.get("effective_date") != right.get("effective_date") or differences:
        return ActionComparison("disputed", ("type_date_or_terms_disagree",) + differences, evidence)
    if not left.get("effective_date") or left.get("issues") or right.get("issues"):
        return ActionComparison("pending", ("incomplete_or_future_action",), evidence)
    if a.get("raw_type") == "cash_dividends" and not a.get("currency"):
        return ActionComparison("pending", ("cash_currency_unproved",), evidence)
    return ActionComparison("verified", (), evidence)


@dataclass(frozen=True, slots=True)
class ActionBridgeProof:
    """Caller-adjudicated identity, field meaning, and full action ledger proof."""
    security_id: str
    identity_evidence: str
    action_terms_evidence: str
    coverage_evidence: str
    source_contract_id: str
    raw_ref: str
    observation_id: str
    revision: int
    available_at: datetime | None
    captured_at: datetime
    action_type: str
    split_ratio: Decimal | None = None
    cash_amount: Decimal | None = None
    currency: str | None = None
    cash_share_basis: str | None = None
    received_security_id: str | None = None
    decision_recorded_at: datetime | None = None


def corporate_action_from_staged(row: Mapping, *, proof: ActionBridgeProof | None,
                                 cutoff: datetime) -> CorporateAction:
    """No publication time, identity, coverage, or canonical type is invented."""
    if proof is None or not all((proof.security_id, proof.identity_evidence, proof.action_terms_evidence,
                                proof.coverage_evidence, proof.source_contract_id, proof.raw_ref)):
        raise ValueError("independently verified identity/action/coverage proof is required")
    require_aware(cutoff, "cutoff")
    effective = row.get("effective_date")
    if effective is None or effective > cutoff.astimezone(NEW_YORK).date():
        raise ValueError("future or unknown action effectiveness")
    if row.get("issues") or row.get("record_kind") != "corporate_action":
        raise ValueError("unresolved staged action cannot cross proof bridge")
    require_aware(proof.available_at, "available_at")
    if proof.available_at is None or proof.available_at > cutoff:
        raise ValueError("independent public availability proof is absent at cutoff")
    fields = row["fields"]
    raw_type = fields.get("raw_type")
    if proof.action_type == "split":
        if raw_type not in SIMPLE_SPLITS or proof.split_ratio is None:
            raise ValueError("unsupported action type or split proof")
        if exact_multiply(_decimal(fields.get("old_rate"), positive=True), proof.split_ratio) != _decimal(fields.get("new_rate"), positive=True):
            raise ValueError("split proof disagrees with source terms")
    elif proof.action_type == "cash_dividend":
        if raw_type != "cash_dividends" or proof.cash_amount != _decimal(fields.get("rate")):
            raise ValueError("cash-dividend proof disagrees with source terms")
        if fields.get("currency") and fields["currency"] != proof.currency:
            raise ValueError("cash-dividend currency disagrees")
    else:
        raise ValueError("unsupported action type requires a separate verified adapter")
    return CorporateAction(id=proof.observation_id, event_id=str(row["row_key"]), security_id=proof.security_id,
        action_type=proof.action_type, revision=proof.revision, available_at=proof.available_at,
        captured_at=proof.captured_at, effective_date=effective, ex_date=_date(fields.get("ex_date")),
        source_contract_id=proof.source_contract_id, raw_ref=proof.raw_ref, split_ratio=proof.split_ratio,
        cash_amount=proof.cash_amount, currency=proof.currency, received_security_id=proof.received_security_id,
        pay_date=_date(fields.get("payable_date")), cash_share_basis=proof.cash_share_basis,
        decision_recorded_at=proof.decision_recorded_at)


@dataclass(frozen=True, slots=True)
class VWAPBridgeProof:
    """Explicit caller evidence for literal scope, identity, and full session."""
    context: PriceContext
    identity_evidence: str
    eligibility_evidence: str
    completeness_evidence: str
    source_contract_id: str
    raw_ref: str
    statistic_id: str
    revision: int
    available_at: datetime
    captured_at: datetime
    materialized_at: datetime
    trade_scope: str
    decision_recorded_at: datetime | None = None


def daily_vwap_stat_from_aggregate(candidate: TradeAggregateCandidate, *,
                                   proof: VWAPBridgeProof | None) -> DailyVWAPStat:
    """Bridge only an adjudicated candidate; REST history never proves replay."""
    if proof is None or not all((proof.identity_evidence, proof.eligibility_evidence, proof.completeness_evidence)):
        raise ValueError("independent identity/eligibility/completion proof is required")
    if not candidate.qualified:
        raise ValueError("unqualified aggregate cannot become exact statistics")
    context = proof.context
    if (proof.trade_scope != candidate.trade_scope or context.feed != "sip" or context.currency != "USD"
            or context.session_scope != "regular" or context.basis != "as_traded"
            or context.quantity_unit != "shares" or context.price_unit != "currency_per_share"
            or not all((context.security_id, context.listing_id, context.venue))):
        raise ValueError("proof context or eligibility scope is incompatible")
    for name in ("available_at", "captured_at", "materialized_at"):
        require_aware(getattr(proof, name), name)
        if getattr(proof, name) is None:
            raise ValueError("proof timestamps must be explicit")
    if proof.available_at < candidate.session_close or proof.available_at > candidate.cutoff:
        raise ValueError("full session completion availability is outside cutoff")
    if (candidate.captured_at is None or proof.captured_at < candidate.captured_at
            or proof.materialized_at < max(proof.captured_at, proof.available_at)):
        raise ValueError("proof capture/materialization precedes input evidence")
    return DailyVWAPStat(proof.statistic_id, context, candidate.session, candidate.sum_price_volume,
        candidate.eligible_volume, candidate.trade_scope, "trade_aggregated", proof.source_contract_id,
        proof.raw_ref, proof.revision, proof.available_at, proof.captured_at, proof.materialized_at,
        proof.decision_recorded_at, no_trade_evidence=proof.completeness_evidence if candidate.eligible_volume == 0 else None,
        historical_replay_supported=False, input_event_ids=candidate.input_event_ids,
        input_signature=candidate.signature)
