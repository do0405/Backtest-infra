"""Versioned exact mappings preserve unmapped source evidence."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from .domain import FinancialFact, SourceFact, stable_hash
from .numeric import NUMERIC_POLICY, deterministic_decimal, exact_multiply


@dataclass(frozen=True, slots=True)
class MappingRule:
    taxonomy: str
    concept: str
    metric: str
    scope: str
    dimensions: dict[str, str] = field(default_factory=dict)
    account_name: str | None = None
    source_unit: str | None = None
    canonical_unit: str | None = None
    multiplier: Decimal = Decimal(1)
    sign_policy: str | None = None
    accounting_basis: str | None = None
    version: str = 'fundamental_mapping_v1'


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    fact: FinancialFact | None
    status: str
    reason: str | None = None
    source_record_ids: tuple[str, ...] = ()
    mapping_version: str | None = None


@deterministic_decimal
def normalize_fact(source: SourceFact, rules: Iterable[MappingRule], *,
                    available_at: datetime | None, captured_at: datetime,
                    period_kind: str | None = None, **metadata) -> NormalizationResult:
    matches = [rule for rule in rules if rule.taxonomy == source.taxonomy and rule.concept == source.concept
               and rule.scope == source.scope and rule.dimensions == source.dimensions
               and (rule.account_name is None or rule.account_name == source.account_name)
               and (rule.source_unit is None or rule.source_unit == source.unit)]
    if not matches:
        return NormalizationResult(None, 'unmapped', 'no_explicit_mapping', (source.id,))
    if len(matches) != 1:
        return NormalizationResult(None, 'ambiguous', 'multiple_explicit_mappings', (source.id,))
    rule = matches[0]
    if source.parse_status != 'ok' or source.is_nil or source.value is None:
        return NormalizationResult(None, 'unavailable', source.parse_status if source.parse_status != 'ok' else 'nil_value', (source.id,), rule.version)
    if source.period_end is None or source.unit is None:
        return NormalizationResult(None, 'unavailable', 'missing_period_or_unit', (source.id,), rule.version)
    value = exact_multiply(source.value,rule.multiplier)
    unit = rule.canonical_unit or source.unit
    currency = metadata.pop('currency', unit.split('/')[0] if unit.split('/')[0] in {'USD','KRW','EUR','JPY','GBP','CAD','AUD','CHF','CNY','HKD'} else None)
    evidence = dict(metadata.pop('evidence', {}))
    evidence.update(mapping_version=rule.version, source_concept=source.concept, source_decimals=source.decimals,
                    source_precision=source.precision, source_sign_policy=rule.sign_policy,
                    source_value=str(source.value), source_unit=source.unit, canonical_unit=unit,
                    normalization_multiplier=str(rule.multiplier),numeric_policy=NUMERIC_POLICY)
    normalized = FinancialFact(id=stable_hash((source.signature, rule.version, rule.metric, unit, rule.multiplier,NUMERIC_POLICY)),
                               metric=rule.metric, value=value, period_kind=period_kind or source.period_kind,
                               period_start=source.period_start, period_end=source.period_end, unit=unit, scope=rule.scope,
                               available_at=available_at, captured_at=captured_at, currency=currency,
                               source_record_ids=(source.id,), dimensions=tuple(sorted(source.dimensions.items())),
                               sign_policy=rule.sign_policy, accounting_basis=metadata.pop('accounting_basis', rule.accounting_basis),
                               evidence=evidence, **metadata)
    return NormalizationResult(normalized, 'mapped', source_record_ids=(source.id,), mapping_version=rule.version)
