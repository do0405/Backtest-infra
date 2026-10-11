"""Pinned standard-concept vocabulary; no implicit fiscal or scope qualification."""

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
import re

from ..db import utcnow
from .domain import SourceFact, stable_hash
from .normalize import MappingRule, NormalizationResult, normalize_fact
from .numeric import NUMERIC_POLICY


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    taxonomy_group: str
    concept: str
    metric: str
    period_kind: str = 'duration'
    unit_kind: str = 'currency'
    sign_policy: str | None = None


@dataclass(frozen=True, slots=True)
class MappingCatalog:
    version: str
    entries: tuple[CatalogEntry, ...]
    namespace_patterns: tuple[tuple[str, str], ...]
    currencies: tuple[str, ...] = ('USD', 'KRW', 'EUR', 'JPY', 'GBP', 'CAD', 'AUD', 'CHF', 'CNY', 'HKD')
    scope_values: tuple[str, ...] = ('unknown', 'consolidated', 'separate', 'parent', 'parent_common')
    policy: str = 'exact_standard_concept_empty_dimensions_preserve_unknown_qualifications_v1'
    numeric_policy: str = NUMERIC_POLICY

    @property
    def content_hash(self) -> str:
        return stable_hash(asdict(self))


@lru_cache(maxsize=2)
def default_catalog(version: str = 'fundamental_mapping_v2') -> MappingCatalog:
    if version not in {'fundamental_mapping_v1','fundamental_mapping_v2'}:
        raise ValueError('unsupported_mapping_catalog_version')
    # Pinned upper bound prevents new taxonomy releases changing v1 acceptance.
    years = '(?:' + '|'.join(str(year) for year in range(2009, 2027)) + ')'
    namespaces = (
        ('us-gaap', r'http://fasb\.org/us-gaap/' + years + r'(?:-\d{2}-\d{2})?'),
        ('ifrs-full', (r'http://' if version=='fundamental_mapping_v1' else r'https?://')
                     + r'xbrl\.ifrs\.org/taxonomy/' + years + r'-\d{2}-\d{2}/ifrs-full'),
    )
    entries = []
    def add(group, concepts, metric, period='duration', unit='currency', sign=None):
        entries.extend(CatalogEntry(group, concept, metric, period, unit, sign) for concept in concepts)
    add('us-gaap', ('RevenueFromContractWithCustomerExcludingAssessedTax',
                    'Revenues', 'SalesRevenueNet'), 'revenue')
    add('us-gaap', ('OperatingIncomeLoss',), 'operating_income')
    add('us-gaap', ('ProfitLoss',), 'net_income_total')
    add('us-gaap', ('NetIncomeLoss',), 'net_income_parent')
    add('us-gaap', ('GrossProfit',), 'gross_profit')
    add('us-gaap', ('NetCashProvidedByUsedInOperatingActivities',), 'cfo')
    add('us-gaap', ('EarningsPerShareBasic',), 'eps_basic', unit='per_share')
    add('us-gaap', ('EarningsPerShareDiluted',), 'eps_diluted', unit='per_share')
    add('us-gaap', ('Assets',), 'assets_total', 'instant')
    add('us-gaap', ('StockholdersEquity',), 'equity_parent', 'instant')
    add('us-gaap', ('IncomeTaxExpenseBenefit',), 'tax_expense')
    add('us-gaap', ('InterestExpense',), 'interest_expense')
    add('us-gaap', ('InvestmentIncomeInterest',), 'interest_income')
    add('us-gaap', ('PaymentsToAcquirePropertyPlantAndEquipment',), 'cash_capex_ppe', sign='positive_outflow')
    add('us-gaap', ('PaymentsToAcquireIntangibleAssets',), 'cash_capex_intangible', sign='positive_outflow')
    add('ifrs-full', ('Revenue',), 'revenue')
    add('ifrs-full', ('ProfitLossFromOperatingActivities',), 'operating_income')
    add('ifrs-full', ('ProfitLoss',), 'net_income_total')
    add('ifrs-full', ('ProfitLossAttributableToOwnersOfParent',), 'net_income_parent')
    add('ifrs-full', ('GrossProfit',), 'gross_profit')
    add('ifrs-full', ('CashFlowsFromUsedInOperatingActivities',), 'cfo')
    add('ifrs-full', ('BasicEarningsLossPerShare',), 'eps_basic', unit='per_share')
    add('ifrs-full', ('DilutedEarningsLossPerShare',), 'eps_diluted', unit='per_share')
    add('ifrs-full', ('Assets',), 'assets_total', 'instant')
    add('ifrs-full', ('EquityAttributableToOwnersOfParent',), 'equity_parent', 'instant')
    add('ifrs-full', ('IncomeTaxExpenseContinuingOperations',), 'tax_expense')
    return MappingCatalog(version, tuple(entries), namespaces)


def taxonomy_group(namespace: str, catalog: MappingCatalog) -> str | None:
    for group, pattern in catalog.namespace_patterns:
        if re.fullmatch(pattern, namespace):
            dated = re.search(r'(20\d{2}-\d{2}-\d{2})', namespace)
            if dated:
                try:
                    date.fromisoformat(dated.group(1))
                except ValueError:
                    return None
            return group
    return None


def local_concept(source: SourceFact) -> str | None:
    if source.concept.startswith('{'):
        namespace, separator, local = source.concept[1:].partition('}')
        return local if separator and namespace == source.taxonomy else None
    pieces = source.concept.split(':')
    if len(pieces) not in {1, 2} or any(not piece for piece in pieces):
        return None
    prefix = pieces[0] if len(pieces) == 2 else None
    bindings = source.evidence.get('namespace_bindings', {})
    if prefix and prefix in bindings and bindings[prefix] != source.taxonomy:
        return None
    return pieces[-1]


def map_source_fact(source: SourceFact, *, accounting_basis: str = 'unknown',
                    catalog: MappingCatalog | None = None, available_at: datetime | None = None,
                    captured_at: datetime | None = None) -> NormalizationResult:
    catalog = catalog or default_catalog()
    group = taxonomy_group(source.taxonomy, catalog)
    concept = local_concept(source)
    entries = [entry for entry in catalog.entries if entry.taxonomy_group == group and entry.concept == concept]
    if source.dimensions or source.scope not in catalog.scope_values:
        return NormalizationResult(None, 'unmapped', 'unsupported_dimensions_or_scope', (source.id,), catalog.version)
    if len(entries) != 1:
        return NormalizationResult(None, 'ambiguous' if entries else 'unmapped',
            'multiple_catalog_entries' if entries else 'unsupported_taxonomy_or_concept', (source.id,), catalog.version)
    entry = entries[0]
    if source.period_kind != entry.period_kind:
        return NormalizationResult(None, 'unmapped', 'unsupported_source_period', (source.id,), catalog.version)
    if (source.period_end is None or source.period_kind == 'duration' and (
        source.period_start is None or source.period_start > source.period_end)):
        return NormalizationResult(None, 'unavailable', 'invalid_source_period', (source.id,), catalog.version)
    if source.value is not None and (not isinstance(source.value, Decimal) or not source.value.is_finite()):
        return NormalizationResult(None, 'unavailable', 'invalid_source_value', (source.id,), catalog.version)
    currency = (source.unit or '').partition('/')[0]
    unit = source.unit
    supported = currency in catalog.currencies and (
        unit == currency if entry.unit_kind == 'currency' else unit in {currency + '/share', currency + '/shares'})
    if not supported:
        return NormalizationResult(None, 'unmapped', 'unsupported_source_unit', (source.id,), catalog.version)
    canonical_unit = currency + '/share' if entry.unit_kind == 'per_share' else currency
    rule = MappingRule(source.taxonomy, source.concept, entry.metric, source.scope,
        source_unit=source.unit, canonical_unit=canonical_unit, sign_policy=entry.sign_policy,
        accounting_basis=accounting_basis, version=catalog.version)
    return normalize_fact(source, (rule,), available_at=available_at, captured_at=captured_at or utcnow(),
        evidence={'mapping_hash': catalog.content_hash, 'mapping_policy': catalog.policy,
                  'taxonomy_group': group, 'source_taxonomy': source.taxonomy,
                  'semantic_qualification': 'unknown', 'source_record_signature': source.signature})
