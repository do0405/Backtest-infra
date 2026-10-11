"""v7 metrics cannot retain superseded canonical or source input revisions."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from backtest_data.catalog.models import Issuer, Security
from backtest_data.db import utcnow
from backtest_data.fundamentals.materialize import materialize_metrics
from backtest_data.fundamentals.models import Filing, FundamentalFact, SourceFinancialFact
from backtest_data.fundamentals.public_availability import append_publication_evidence, numeric_binding
from backtest_data.fundamentals.publication_models import FinancialPublicationEvidence
from backtest_data.fundamentals.storage import store_extraction
from backtest_data.fundamentals.xbrl import parse_xbrl
from backtest_data.ingestion.models import TargetUniverseManifest
from backtest_data.releases.build import build_release
from backtest_data.releases.models import SelectionDecision
from backtest_data.releases.query import get_financial_metrics
from backtest_data.releases.query_context import QueryContext

pytestmark = pytest.mark.integration
CAPTURE = datetime(2026, 10, 1, tzinfo=timezone.utc)
PUBLIC = datetime(2024, 3, 2, 5, tzinfo=timezone.utc)
CORRECTION = datetime(2024, 3, 6, 5, tzinfo=timezone.utc)
GROWTH = 'growth_revenue_fy_yoy'


def fixture_target(session):
    issuer = Issuer(market='US', source_identifier='CIK0000010000', name='Metric revision fixture')
    session.add(issuer)
    session.flush()
    security = Security(issuer_id=issuer.id, market='US', source_identifier=str(uuid4()),
        name='Metric revision fixture', security_type='common_stock')
    session.add(security)
    session.flush()
    target = TargetUniverseManifest(content_hash=uuid4().hex * 2,
        scope={'historical_universe_complete': False},
        members=[{'security_id': security.id, 'issuer_id': issuer.id, 'market': 'US'}], gaps=[])
    session.add(target)
    session.flush()
    return issuer, target


def fixture_input(session, issuer, value, public_day, *, year=2023, quarter=None,
                  qualified=True, canonical=True, fiscal_qualified=True,
                  taxonomy='http://fasb.org/us-gaap/2024', accounting_basis='US_GAAP',
                  period_updates=None, basis_updates=None,
                  concept='RevenueFromContractWithCustomerExcludingAssessedTax'):
    starts = (date(year, 1, 1), date(year, 4, 1), date(year, 7, 1), date(year, 10, 1))
    ends = (date(year, 3, 31), date(year, 6, 30), date(year, 9, 30), date(year, 12, 31))
    start, end = (starts[quarter - 1], ends[quarter - 1]) if quarter else (starts[0], ends[-1])
    accession = '0000010000-24-' + str(uuid4().int % 1000000).zfill(6)
    xml = f'''<xbrl xmlns="http://www.xbrl.org/2003/instance"
        xmlns:g="{taxonomy}" xmlns:i="http://www.xbrl.org/2003/iso4217">
        <context id="C"><entity><identifier scheme="http://www.sec.gov/CIK">10000</identifier></entity>
        <period><startDate>{start}</startDate><endDate>{end}</endDate></period></context>
        <unit id="USD"><measure>i:USD</measure></unit>
        <g:{concept} contextRef="C" unitRef="USD" decimals="0">{value}</g:{concept}></xbrl>'''.encode()
    document = store_extraction(session, issuer_id=issuer.id, source='sec', accession=accession,
        form='10-Q' if quarter else '10-K', source_url='https://www.sec.gov/original.xml',
        content=xml, result=parse_xbrl(xml), available_at=CAPTURE, captured_at=CAPTURE,
        accounting_basis=accounting_basis)
    filing = session.get(Filing, document.filing_id)
    source = session.scalar(select(SourceFinancialFact).where(SourceFinancialFact.document_id == document.id))
    if qualified:
        binding = numeric_binding(document, [source])
        url = (f'https://www.sec.gov/Archives/edgar/Feed/{public_day.year}/'
            f'QTR{(public_day.month - 1) // 3 + 1}/{public_day.strftime("%Y%m%d")}.nc.tar.gz')
        append_publication_evidence(session, document=document, filing=filing, status='qualified',
            public_day=public_day, captured_at=utcnow(), recorded_at=utcnow(), evidence={
                'binding': {'document_id': document.id, 'filing_id': filing.id, 'issuer_id': issuer.id,
                    'source': 'sec', 'accession': accession, 'document_hash': document.content_hash},
                'proof': {'kind': 'sec_dated_feed_original', 'public_day': public_day.isoformat(),
                    'issuer_id': issuer.id, 'accession': accession, 'source_url': url,
                    'source_body_hash': 'a' * 64, 'original_body_hash': document.content_hash,
                    'membership_verified': True, 'original_version_verified': True,
                    'member_hash': document.content_hash, 'member_name': 'original.xml', 'archive_url': url},
                'numeric_binding': {**binding, 'original_digest': binding['sql_digest']}})
    if canonical:
        period = {'kind': 'quarter' if quarter else 'annual', 'start': start.isoformat(),
            'end': end.isoformat(), 'fiscal_year': year, 'fiscal_cycle': str(year)}
        if quarter:
            period.update(fiscal_quarter=quarter, quarter_index=year * 4 + quarter - 1)
        if not fiscal_qualified:
            period = {'kind': 'duration', 'start': start.isoformat(), 'end': end.isoformat()}
        period.update(period_updates or {})
        fact = FundamentalFact(issuer_id=issuer.id, source_fact_id=source.id, metric_id='revenue',
            mapping_version='fundamental_mapping_v1', value=source.value, unit='USD', period=period,
            basis={'scope': 'consolidated', 'currency': 'USD', 'accounting_basis': accounting_basis,
                'fiscal_lineage': 'fixture-calendar' if fiscal_qualified else None,
                'qualification_evidence': ['fixture:verified-fiscal-context'] if fiscal_qualified else [],
                **(basis_updates or {})},
            available_at=CAPTURE, captured_at=CAPTURE, materialized_at=utcnow())
        session.add(fact)
        session.flush()
        session.add(SelectionDecision(record_domain='fundamental_fact', record_id=fact.id,
            status='accepted', rule_version='fundamental_mapping_v1', evidence_grade='research_only',
            reason='fixture qualification', actor='test', recorded_at=utcnow()))
        session.flush()
    return document


def fixture_annual_growth(session, *, query_version='release_query_v7', evaluation=PUBLIC,
                          taxonomy='http://fasb.org/us-gaap/2024'):
    issuer, target = fixture_target(session)
    documents = [fixture_input(session, issuer, '100', date(2023, 3, 1), year=2022, taxonomy=taxonomy),
        fixture_input(session, issuer, '110', date(2024, 3, 1), taxonomy=taxonomy)]
    report = materialize_metrics(session, as_of=evaluation, issuer_ids=[issuer.id],
        metric_ids=[GROWTH], query_version=query_version)
    assert report.successful == 1
    return issuer, target, documents


def seal(session, target, documents, query_version='release_query_v7'):
    verified = len(set(session.scalars(select(FinancialPublicationEvidence.document_id).where(
        FinancialPublicationEvidence.document_id.in_([document.id for document in documents]),
        FinancialPublicationEvidence.status == 'qualified'))))
    return build_release(session, target.id, raw_root=Path('artifacts/tests/no-raw'),
        query_version=query_version, financial_scope={
            'document_ids': [document.id for document in documents],
            'audit_watermark': utcnow() + timedelta(seconds=1),
            'audit_counts': {'total_documents': len(documents),
                'verified_documents': verified, 'missing_documents': len(documents) - verified}})['release_id']


def metric(session, release, issuer, cutoff, metric_id=GROWTH, mode='reconstructed_public'):
    context = QueryContext(release, cutoff, mode, 'fundamentals_sql', 'periodic_report', True)
    return get_financial_metrics(session, [issuer.id], context, [metric_id])['rows'][0]


@pytest.mark.parametrize('query_version', ['release_query_v6', 'release_query_v7'])
def test_certified_correction_requires_recomputed_metric_only_in_v7(db_session, query_version):
    issuer, target, documents = fixture_annual_growth(db_session, query_version=query_version)
    documents.append(fixture_input(db_session, issuer, '150', date(2024, 3, 5)))
    release = seal(db_session, target, documents, query_version)
    assert metric(db_session, release, issuer, PUBLIC)['value'] == '0.1'
    after = metric(db_session, release, issuer, CORRECTION)
    if query_version == 'release_query_v7':
        assert after['status'] == 'unavailable' and after['value'] is None
    else:
        assert after['value'] == '0.1'


def test_correct_recompute_beats_stale_larger_evaluation_as_of(db_session):
    issuer, target, documents = fixture_annual_growth(db_session,
        evaluation=datetime(2026, 10, 5, tzinfo=timezone.utc))
    documents.append(fixture_input(db_session, issuer, '150', date(2024, 3, 5)))
    report = materialize_metrics(db_session, as_of=CORRECTION, issuer_ids=[issuer.id],
        metric_ids=[GROWTH], query_version='release_query_v7')
    assert report.successful == 1
    release = seal(db_session, target, documents)
    assert metric(db_session, release, issuer, PUBLIC)['value'] == '0.1'
    assert metric(db_session, release, issuer, CORRECTION)['value'] == '0.5'


def test_uncertified_correction_cannot_supersede_certified_metric(db_session):
    issuer, target, documents = fixture_annual_growth(db_session)
    documents.append(fixture_input(db_session, issuer, '150', date(2024, 3, 5), qualified=False))
    release = seal(db_session, target, documents)
    assert metric(db_session, release, issuer, utcnow())['value'] == '0.1'


def test_equivalent_same_bound_duplicate_does_not_invalidate_metric(db_session):
    issuer, target, documents = fixture_annual_growth(db_session)
    documents.append(fixture_input(db_session, issuer, '110', date(2024, 3, 1)))
    release = seal(db_session, target, documents)
    assert metric(db_session, release, issuer, PUBLIC)['value'] == '0.1'


@pytest.mark.parametrize('period_updates,basis_updates', [
    ({'fiscal_year': 2024}, {}),
    ({'fiscal_quarter': 4}, {}),
    ({'quarter_index': 2024 * 4 + 3}, {}),
    ({'fiscal_cycle': '2024'}, {}),
    ({}, {'directly_reported': False}),
    ({}, {'period_compatible': False}),
    ({}, {'sign_policy': 'positive_outflow'}),
    ({}, {'component_ids': ['different_revenue_component']}),
])
def test_same_bound_equal_value_conflicting_arithmetic_qualification_is_ambiguous(
        db_session, period_updates, basis_updates):
    issuer, target, documents = fixture_annual_growth(db_session)
    documents.append(fixture_input(db_session, issuer, '110', date(2024, 3, 1),
        period_updates=period_updates, basis_updates=basis_updates))
    release = seal(db_session, target, documents)
    result = metric(db_session, release, issuer, PUBLIC)
    assert result['status'] == 'unavailable' and result['value'] is None


def test_latest_arithmetic_qualification_requires_compatible_recomputation(db_session):
    issuer, target, documents = fixture_annual_growth(db_session)
    documents.append(fixture_input(db_session, issuer, '110', date(2024, 3, 5),
        concept='SalesRevenueNet', period_updates={'fiscal_year': 2024, 'fiscal_cycle': '2024'}))
    release = seal(db_session, target, documents)
    assert metric(db_session, release, issuer, PUBLIC)['value'] == '0.1'
    result = metric(db_session, release, issuer, CORRECTION)
    assert result['status'] == 'unavailable' and result['value'] is None
    # The latest FY labels lack an adjacent prior FY; a fresh calculation must
    # preserve that unavailable result rather than revive the older labels.
    materialize_metrics(db_session, as_of=CORRECTION, issuer_ids=[issuer.id],
        metric_ids=[GROWTH], query_version='release_query_v7')
    latest_release = seal(db_session, target, documents)
    result = metric(db_session, latest_release, issuer, CORRECTION)
    assert result['status'] == 'unavailable' and result['reason'] == 'missing_comparison_period'


def test_metadata_gate_visits_only_selected_proofs_and_missing_ids_fail_closed(monkeypatch):
    from backtest_data.fundamentals import completion
    from backtest_data.releases import public_clock
    from backtest_data.releases.query import QueryRows, visible
    from backtest_data.releases.query_context import policy_bundle
    body_hash = 'd' * 64
    proofs = {str(index): {'id': str(index), 'status': 'qualified', 'document_hash': body_hash,
        'evidence': {'proof': {'original_body_hash': body_hash}}} for index in range(100)}
    mapping = FundamentalFact(id='mapping', issuer_id='issuer', source_fact_id='source',
        basis={'sec_fiscal_metadata': {'status': 'reported'}, 'source_document_hash': body_hash},
        available_at=PUBLIC, captured_at=CAPTURE, materialized_at=CAPTURE)
    rows = QueryRows({'financial_publication_evidence': proofs, 'fundamental_fact': {'mapping': mapping}},
        {'policies': policy_bundle('release_query_v7')})
    resolution = {'status': 'qualified', 'evidence_ids': ['0'], 'effective_available_at': PUBLIC}
    monkeypatch.setattr(public_clock, 'row_availability', lambda *args, **kwargs: resolution)
    original_gate = completion.original_metadata_qualified
    examined = []

    def count_gate(basis, evidence, selected):
        evidence = list(evidence)
        examined.append([proof['id'] for proof in evidence])
        return original_gate(basis, evidence, selected)

    monkeypatch.setattr(completion, 'original_metadata_qualified', count_gate)
    context = QueryContext('release', PUBLIC, 'reconstructed_public', 'fundamentals_sql', 'periodic_report', True)
    assert visible(mapping, context, rows)
    assert examined == [['0']]
    resolution['evidence_ids'] = ['0', 'missing']
    assert not visible(mapping, context, rows)


def test_observed_system_also_rejects_superseded_metric_after_actual_capture(db_session):
    issuer, target, documents = fixture_annual_growth(db_session)
    original_release = seal(db_session, target, documents)
    assert metric(db_session, original_release, issuer, utcnow(), mode='observed_system')['value'] == '0.1'
    documents.append(fixture_input(db_session, issuer, '150', date(2024, 3, 5)))
    corrected_release = seal(db_session, target, documents)
    result = metric(db_session, corrected_release, issuer, utcnow(), mode='observed_system')
    assert result['status'] == 'unavailable' and result['value'] is None


def test_v5_metric_selection_is_unchanged(db_session):
    issuer, target, documents = fixture_annual_growth(db_session,
        query_version='release_query_v5', evaluation=utcnow())
    documents.append(fixture_input(db_session, issuer, '150', date(2024, 3, 5)))
    release = build_release(db_session, target.id, raw_root=Path('artifacts/tests/no-raw'),
        query_version='release_query_v5')['release_id']
    assert metric(db_session, release, issuer, utcnow())['value'] == '0.1'


def test_same_public_bound_conflict_invalidates_precomputed_metric(db_session):
    issuer, target, documents = fixture_annual_growth(db_session)
    documents.append(fixture_input(db_session, issuer, '150', date(2024, 3, 1)))
    release = seal(db_session, target, documents)
    result = metric(db_session, release, issuer, PUBLIC)
    assert result['status'] == 'unavailable' and result['value'] is None


@pytest.mark.parametrize('dependency', ['source', 'canonical'])
def test_both_source_and_canonical_revision_groups_are_checked(db_session, dependency):
    issuer, target, documents = fixture_annual_growth(db_session)
    documents.append(fixture_input(db_session, issuer, '150', date(2024, 3, 5),
        canonical=dependency != 'source', concept='SalesRevenueNet' if dependency == 'canonical'
            else 'RevenueFromContractWithCustomerExcludingAssessedTax'))
    release = seal(db_session, target, documents)
    result = metric(db_session, release, issuer, CORRECTION)
    assert result['status'] == 'unavailable' and result['value'] is None


@pytest.mark.parametrize('query_version', ['release_query_v6', 'release_query_v7'])
@pytest.mark.parametrize('correction_mapping', ['absent', 'unqualified_duration'])
def test_approved_taxonomy_version_correction_cannot_evade_source_gate(
        db_session, query_version, correction_mapping):
    issuer, target, documents = fixture_annual_growth(db_session, query_version=query_version,
        taxonomy='http://fasb.org/us-gaap/2023')
    documents.append(fixture_input(db_session, issuer, '150', date(2025, 3, 1),
        taxonomy='http://fasb.org/us-gaap/2025', canonical=correction_mapping != 'absent',
        fiscal_qualified=False))
    release = seal(db_session, target, documents, query_version)
    bound = datetime(2025, 3, 2, 5, tzinfo=timezone.utc)
    assert metric(db_session, release, issuer, bound - timedelta(seconds=1))['value'] == '0.1'
    result = metric(db_session, release, issuer, bound)
    if query_version == 'release_query_v7':
        assert result['status'] == 'unavailable' and result['value'] is None
    else:
        assert result['value'] == '0.1'


@pytest.mark.parametrize('taxonomy,accounting_basis,concept', [
    ('http://issuer.example/us-gaap/2025', 'US_GAAP', 'RevenueFromContractWithCustomerExcludingAssessedTax'),
    ('http://fasb.org/us-gaap/2028', 'US_GAAP', 'RevenueFromContractWithCustomerExcludingAssessedTax'),
    ('http://fasb.org/us-gaap/2025', 'IFRS', 'RevenueFromContractWithCustomerExcludingAssessedTax'),
    ('http://fasb.org/us-gaap/2025', 'US_GAAP', 'OperatingIncomeLoss'),
])
def test_source_family_does_not_merge_unrelated_namespace_basis_or_account(
        db_session, taxonomy, accounting_basis, concept):
    issuer, target, documents = fixture_annual_growth(db_session, taxonomy='http://fasb.org/us-gaap/2023')
    documents.append(fixture_input(db_session, issuer, '150', date(2025, 3, 1),
        canonical=False, taxonomy=taxonomy, accounting_basis=accounting_basis, concept=concept))
    release = seal(db_session, target, documents)
    assert metric(db_session, release, issuer, datetime(2025, 3, 2, 5, tzinfo=timezone.utc))['value'] == '0.1'


def test_ttm_revalidates_every_quarter_dependency_at_correction_cutoff(db_session):
    issuer, target = fixture_target(db_session)
    documents = [fixture_input(db_session, issuer, str(quarter * 10), day, quarter=quarter)
        for quarter, day in enumerate((date(2023, 5, 1), date(2023, 8, 1),
            date(2023, 11, 1), date(2024, 3, 1)), start=1)]
    first = materialize_metrics(db_session, as_of=PUBLIC, issuer_ids=[issuer.id],
        metric_ids=['revenue_ttm'], query_version='release_query_v7')
    assert first.successful == 1
    documents.append(fixture_input(db_session, issuer, '50', date(2024, 3, 5), quarter=2))
    release = seal(db_session, target, documents)
    assert Decimal(metric(db_session, release, issuer, PUBLIC, 'revenue_ttm')['value']) == 100
    assert metric(db_session, release, issuer, CORRECTION, 'revenue_ttm')['status'] == 'unavailable'
    materialize_metrics(db_session, as_of=CORRECTION, issuer_ids=[issuer.id],
        metric_ids=['revenue_ttm'], query_version='release_query_v7')
    corrected_release = seal(db_session, target, documents)
    assert Decimal(metric(db_session, corrected_release, issuer, CORRECTION, 'revenue_ttm')['value']) == 130
