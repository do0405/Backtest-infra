"""Pure financial evidence and calculation inputs, independent of SQL/HTTP."""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json


def aware(value: datetime | None, field_name: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f'{field_name} requires a timezone-aware timestamp')


def stable_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, default=str, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Filing:
    id: str
    source: str
    accession: str
    available_at: datetime | None
    captured_at: datetime
    published_at: datetime | None = None
    publication_precision: str = 'unknown'
    revision_kind: str = 'original'
    content_hash: str | None = None
    accounting_basis: str | None = None
    assurance: str | None = None

    def __post_init__(self) -> None:
        for name in ('available_at', 'captured_at', 'published_at'):
            aware(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class Context:
    id: str
    entity_identifier: str | None = None
    entity_scheme: str | None = None
    period_kind: str = 'unknown'
    period_start: date | None = None
    period_end: date | None = None
    dimensions: dict[str, str] = field(default_factory=dict)
    raw_xml: str | None = None
    scope: str = 'unknown'
    evidence: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourceFact:
    id: str
    concept: str
    taxonomy: str
    context_id: str
    value: Decimal | None
    raw_value: str
    unit: str | None
    original_unit: str | None
    account_name: str | None = None
    dimensions: dict[str, str] = field(default_factory=dict)
    period_kind: str = 'unknown'
    period_start: date | None = None
    period_end: date | None = None
    scope: str = 'unknown'
    decimals: str | None = None
    precision: str | None = None
    scale: int | None = None
    sign: str | None = None
    is_nil: bool = False
    format: str | None = None
    parse_status: str = 'ok'
    raw_xml: str | None = None
    source_node_ids: tuple[str, ...] = ()
    evidence: dict[str, object] = field(default_factory=dict)

    @property
    def signature(self) -> str:
        return stable_hash((self.taxonomy, self.concept, self.context_id, self.dimensions,
                            self.period_kind, self.period_start, self.period_end,
                            self.original_unit, self.value, self.raw_value, self.decimals,
                            self.precision, self.is_nil, self.scale, self.sign, self.format,
                            self.unit,self.evidence.get('numeric_policy')))


@dataclass(frozen=True, slots=True)
class ExtractionIssue:
    code: str
    node_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class ExtractionInventory:
    numeric_nodes: int
    extracted_nodes: int
    context_ids: tuple[str, ...]
    unit_ids: tuple[str, ...]
    deduplicated_nodes: int = 0
    rejected_nodes: int = 0
    unsupported_nodes: int = 0
    emitted_bytes: int = 0
    namespace_entries: int = 0
    namespace_scopes: int = 0


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    facts: tuple[SourceFact, ...]
    contexts: dict[str, Context]
    units: dict[str, str]
    inventory: ExtractionInventory
    issues: tuple[ExtractionIssue, ...] = ()
    parser_version: str = 'financial_extraction_v1'

    @property
    def complete(self) -> bool:
        return not self.issues and self.inventory.numeric_nodes == self.inventory.extracted_nodes


@dataclass(frozen=True, slots=True)
class FinancialFact:
    """Explicit period lineage avoids calendar-quarter and scope guesses.

    quarter_index is monotonic within a fiscal lineage; consecutive actual
    quarters differ by one. annual facts use fiscal_year and period_kind=annual.
    source_record_ids preserve the closure of normalized/derived evidence.
    """
    id: str
    metric: str
    value: Decimal | None
    period_kind: str
    period_start: date | None
    period_end: date
    unit: str
    scope: str
    available_at: datetime | None
    captured_at: datetime
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None
    quarter_index: int | None = None
    fiscal_cycle: str | None = None
    fiscal_lineage: str | None = None
    currency: str | None = None
    share_basis: str | None = None
    directly_reported: bool = True
    period_compatible: bool = True
    source_record_ids: tuple[str, ...] = ()
    first_seen_at: datetime | None = None
    extracted_at: datetime | None = None
    decision_recorded_at: datetime | None = None
    materialized_at: datetime | None = None
    revision: int = 0
    release_id: str | None = None
    accounting_basis: str | None = None
    dimensions: tuple[tuple[str, str], ...] = ()
    sign_policy: str | None = None
    component_ids: tuple[str, ...] = ()
    evidence: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ('available_at', 'captured_at', 'first_seen_at', 'extracted_at',
                     'decision_recorded_at', 'materialized_at'):
            aware(getattr(self, name), name)
        if self.value is not None and (not isinstance(self.value, Decimal) or not self.value.is_finite()):
            raise ValueError('value requires a finite Decimal')

    @property
    def input_ids(self) -> tuple[str, ...]:
        return self.source_record_ids or (self.id,)


@dataclass(frozen=True, slots=True)
class MetricResult:
    metric_id: str
    value: Decimal | None
    status: str
    reason: str | None
    unit: str
    input_record_ids: tuple[str, ...] = ()
    available_at: datetime | None = None
    materialized_at: datetime | None = None
    definition_version: str = 'financial_metrics_v1'
    scope: str | None = None
    currency: str | None = None
    share_basis: str | None = None
    flags: tuple[str, ...] = ()
    absolute_delta: Decimal | None = None
    evidence: dict[str, object] = field(default_factory=dict)
