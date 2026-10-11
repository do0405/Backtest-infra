"""Bounded SQL coverage snapshots; never infer completeness from job termination."""
from collections import defaultdict
from datetime import date
from sqlalchemy import and_, case, cast, false, func, literal_column, or_, select, String, text, true
from sqlalchemy.dialects.postgresql import insert

from ..catalog.models import Security
from ..db import new_id, transaction, utcnow
from ..fundamentals.domain import stable_hash
from ..fundamentals.models import Filing, FinancialSourceDocument, FundProductFact, SourceFinancialFact
from ..fundamentals.public_availability import resolve_document_availability
from ..fundamentals.publication_models import FinancialPublicationEvidence
from ..ingestion.models import CollectionJob, TargetUniverseManifest
from ..prices.models import CorporateActionObservation, PriceObservation, VWAPDailyStat
from ..releases.models import SelectionDecision
from .models import CoverageFinancialPeriod, CoverageGap, CoverageMember, CoverageRun, CoverageSummary
from .policy import (EXPECTED_MARKET_COUNTS, POLICY_VERSION, company_fundamentals_applicability,
    market_cutoff, missing_date_spans, provisional_weekdays, validate_bounds)


SUPPLEMENTAL_READINESS = {"corporate_action_ledger_coverage": "unavailable",
    "historical_listing_identity": "unknown", "etf_etn_product_data": "observed_only",
    "index_membership_expected_universe": "unknown"}


def start_coverage_run(session, manifest_id, *, output_start=date(2016, 1, 1),
        output_end=date(2026, 10, 7), watermark=None, expected_market_counts=None):
    """Snapshot every manifest member in PostgreSQL, including broken references."""
    watermark = watermark or utcnow()
    validate_bounds(output_start, output_end, watermark)
    expected = dict(EXPECTED_MARKET_COUNTS if expected_market_counts is None else expected_market_counts)
    if any(type(value) is not int or value < 0 for value in expected.values()):
        raise ValueError("expected market counts must be nonnegative integers")
    manifest = session.execute(select(TargetUniverseManifest.id, TargetUniverseManifest.content_hash,
        TargetUniverseManifest.scope, TargetUniverseManifest.gaps, func.jsonb_array_length(TargetUniverseManifest.members))
        .where(TargetUniverseManifest.id == manifest_id)).first()
    if manifest is None or manifest[4] <= 0:
        raise ValueError("nonempty target manifest required")
    identity = new_id()
    session.add(CoverageRun(id=identity, manifest_id=manifest.id, manifest_hash=manifest.content_hash,
        policy_version=POLICY_VERSION, output_start=output_start, output_end=output_end, watermark=watermark,
        target_count=manifest[4], processed_count=0, cursor=0, status="pending",
        details={"expected_market_counts": expected, "manifest_scope": manifest.scope,
            "manifest_gaps": manifest.gaps, "calendar_basis": "provisional_weekdays",
            "historical_eligibility": "unknown", "financial_period_expectations": "unknown",
            "source_visibility": "capture_and_recording_watermark",
            "collector_visibility": "status_at_member_audit_time",
            "supplemental_readiness": dict(SUPPLEMENTAL_READINESS)}))
    session.flush()
    session.execute(text("""INSERT INTO coverage_member
        (id, run_id, ordinal, security_id, market, member, done)
        SELECT md5(:run_id || ':' || expanded.ordinality::text)::uuid::text,
            :run_id, expanded.ordinality, nullif(expanded.value->>'security_id', ''),
            coalesce(expanded.value->>'market', 'unknown'), expanded.value, false
        FROM target_universe_manifest AS manifest,
            LATERAL jsonb_array_elements(manifest.members) WITH ORDINALITY AS expanded(value, ordinality)
        WHERE manifest.id = :manifest_id"""), {"run_id": identity, "manifest_id": manifest_id})
    return identity


def _latest_status(model, domain, watermark):
    return select(SelectionDecision.status).where(SelectionDecision.record_domain == domain,
        SelectionDecision.record_id == model.id, SelectionDecision.recorded_at <= watermark).order_by(
            SelectionDecision.recorded_at.desc(), SelectionDecision.id.desc()).limit(1).scalar_subquery()


# Fixed literals let PostgreSQL prove the partial-index predicate even for a
# generic prepared plan. Keep the immutable migration's predicate identical.
# Source Numeric values are tested here, never stored as index payloads.
PRICE_STRUCTURE_PREDICATE = """basis = 'as_traded' AND quantity_unit = 'shares'
    AND open > 0 AND close > 0 AND low > 0 AND high >= low
    AND high >= open AND high >= close AND low <= open AND low <= close
    AND volume IS NOT NULL AND volume >= 0
    AND CAST(open AS VARCHAR) NOT IN ('NaN', 'Infinity', '-Infinity')
    AND CAST(high AS VARCHAR) NOT IN ('NaN', 'Infinity', '-Infinity')
    AND CAST(low AS VARCHAR) NOT IN ('NaN', 'Infinity', '-Infinity')
    AND CAST(close AS VARCHAR) NOT IN ('NaN', 'Infinity', '-Infinity')
    AND CAST(volume AS VARCHAR) NOT IN ('NaN', 'Infinity', '-Infinity')"""


def _price_dates_statement(run, security_ids):
    model = PriceObservation
    bounds = (model.security_id.in_(security_ids), model.session >= run.output_start,
              model.session <= run.output_end, model.captured_at <= run.watermark)
    observed = select(model.security_id, model.session, func.count().label("rows")).where(
        *bounds).group_by(model.security_id, model.session).cte("observed_price_dates")
    latest = select(SelectionDecision.status, SelectionDecision.rule_version, SelectionDecision.evidence_grade).where(
        SelectionDecision.record_domain == literal_column("'price_observation'"),
        SelectionDecision.record_id == model.id, SelectionDecision.recorded_at <= run.watermark).order_by(
            SelectionDecision.recorded_at.desc(), SelectionDecision.id.desc()).limit(1).lateral("latest_price_decision")
    clean = select(model.security_id, model.session, func.count().label("clean_rows")).select_from(
        model.__table__.join(latest, true())).where(*bounds, text(PRICE_STRUCTURE_PREDICATE),
            latest.c.status == "accepted", latest.c.evidence_grade == "contract_verified",
            latest.c.rule_version.in_(("raw_price_structure_v1", "alpaca_raw_price_structure_v1",
                                      "kis_overseas_raw_price_structure_v1"))).group_by(
                                          model.security_id, model.session).cte("clean_price_dates").prefix_with(
                                              "MATERIALIZED", dialect="postgresql")
    # Both reads share one MVCC snapshot. Observed-only dates and every capture
    # remain in the denominator even when the latest decision rejects a quote.
    # Materialize the small date aggregate once: generic plans can otherwise
    # rescan the raw clean index and its decision lookups for every outer date.
    return select(observed.c.security_id, observed.c.session, observed.c.rows,
        func.coalesce(clean.c.clean_rows, 0).label("clean_rows")).select_from(observed.outerjoin(clean,
            and_(observed.c.security_id == clean.c.security_id, observed.c.session == clean.c.session)))


def _dates(session, run, security_ids, domain):
    if not security_ids:
        return {}
    if domain == "prices":
        statement = _price_dates_statement(run, security_ids)
    else:
        model = VWAPDailyStat
        valid = _latest_status(model, "vwap_daily_stat", run.watermark) == "accepted"
        valid = and_(valid, model.basis == "as_traded", model.eligible_volume > 0,
            model.sum_price_volume >= 0,
            model.method.in_(("matched_daily_statistics", "trade_aggregated")),
            model.evidence["matched_scope_verified"].astext == "true",
            model.evidence["numerator_trade_scope"].astext == model.trade_scope,
            model.evidence["denominator_trade_scope"].astext == model.trade_scope,
            model.materialized_at <= run.watermark,
            cast(model.eligible_volume, String).not_in(("NaN", "Infinity", "-Infinity")),
            cast(model.sum_price_volume, String).not_in(("NaN", "Infinity", "-Infinity")))
        statement = select(model.security_id, model.session, func.count().label("rows"),
            func.count().filter(valid).label("clean_rows")).where(model.security_id.in_(security_ids),
            model.session >= run.output_start, model.session <= run.output_end,
            model.captured_at <= run.watermark).group_by(model.security_id, model.session)
    result = defaultdict(lambda: {"observed": set(), "clean": set(), "source_rows": 0})
    for row in session.execute(statement).yield_per(500):
        value = result[row.security_id]
        value["observed"].add(row.session)
        value["source_rows"] += row.rows
        if row.clean_rows:
            value["clean"].add(row.session)
    return result


def _collectors(session, securities):
    entity_ids = set(securities) | {value.issuer_id for value in securities.values() if value.issuer_id}
    result = defaultdict(list)
    if not entity_ids:
        return result
    statement = select(CollectionJob.entity_id, CollectionJob.source, CollectionJob.domain,
        CollectionJob.status, func.count().label("count"),
        func.count().filter(CollectionJob.checkpoint["traversal_complete"].astext == "true").label("traversal"),
        func.count().filter(CollectionJob.checkpoint["source_exhausted"].astext == "true").label("exhausted"))
    for row in session.execute(statement.where(CollectionJob.entity_id.in_(entity_ids),
            CollectionJob.status != "superseded").group_by(CollectionJob.entity_id, CollectionJob.source,
                CollectionJob.domain, CollectionJob.status)):
        result[row.entity_id].append({"source": row.source, "domain": row.domain, "status": row.status,
            "count": row.count, "traversal_complete_jobs": row.traversal, "source_exhausted_jobs": row.exhausted})
    return result


def _collector_summary(values, domain):
    jobs = [item for item in values if item["domain"] == domain]
    return {"jobs": jobs, "job_count": sum(item["count"] for item in jobs),
        "unsupported_jobs": sum(item["count"] for item in jobs if item["status"] == "unsupported"),
        "traversal_complete_jobs": sum(item["traversal_complete_jobs"] for item in jobs),
        "source_exhausted_jobs": sum(item["source_exhausted_jobs"] for item in jobs),
        "coverage_certified_by_termination": False}


def _supplemental_counts(session, run, security_ids):
    if not security_ids:
        return {}, {}
    actions = dict(session.execute(select(CorporateActionObservation.security_id,
        func.count(func.distinct(CorporateActionObservation.event_id))).where(
            CorporateActionObservation.security_id.in_(security_ids),
            CorporateActionObservation.effective_session >= run.output_start,
            CorporateActionObservation.effective_session <= run.output_end,
            CorporateActionObservation.captured_at <= run.watermark).group_by(CorporateActionObservation.security_id)).all())
    cutoff = case((Security.market == "KR", market_cutoff(run.output_end, "KR")),
        else_=market_cutoff(run.output_end, "US"))
    products = dict(session.execute(select(FundProductFact.security_id, func.count()).join(
        Security, Security.id == FundProductFact.security_id).where(FundProductFact.security_id.in_(security_ids),
            FundProductFact.captured_at <= run.watermark, FundProductFact.available_at <= cutoff)
        .group_by(FundProductFact.security_id)).all())
    return actions, products


def _supplemental_readiness(session, run, member, security, actions, products):
    _gap(session, run, member, "corporate_actions", "corporate_actions_verified_coverage_unavailable", 1,
        observed_event_count=actions.get(member.security_id, 0),
        ledger_coverage="unavailable", observations_do_not_certify_no_event_intervals=True,
        no_future_actions_applied=True)
    _gap(session, run, member, "security_identity", "historical_listing_identity_unknown", 1,
        historical_lifecycle_source="not_qualified", current_directory_is_not_listing_history=True)
    kind = security.security_type if security else member.member.get("security_type")
    if company_fundamentals_applicability(kind) == "not_applicable":
        count = products.get(member.security_id, 0)
        _gap(session, run, member, "fund_products", "etf_etn_product_facts_missing" if not count else
            "etf_etn_product_coverage_unqualified", 1, observed_product_fact_count=count,
            expected_product_metrics="unknown", coverage_complete=False)


def _gap(session, run, member, domain, reason, count=None, **details):
    session.add(CoverageGap(run_id=run.id, member_id=member.id, market=member.market, domain=domain,
        reason=reason, count=count, details=details))


def _summary(session, run, member, domain, observed=0, clean=0, expected=None, missing=None, **details):
    session.add(CoverageSummary(run_id=run.id, member_id=member.id, market=member.market, domain=domain,
        observed_count=observed, clean_count=clean, expected_count=expected, missing_count=missing,
        coverage_complete=False, details=details))


def _daily_summary(session, run, member, domain, dates, collector, expected):
    observed, clean = dates.get("observed", set()), dates.get("clean", set())
    missing = missing_date_spans(expected, clean)
    count = sum(item["count"] for item in missing)
    _summary(session, run, member, domain, len(observed), len(clean), len(expected), count,
        collector=collector, expected_basis="provisional_weekdays", official_expected_count=None,
        eligibility="unknown", clean_scope="accepted_source_structure",
        source_row_count=dates.get("source_rows", 0), missing_date_spans=missing,
        observed_first=min(observed).isoformat() if observed else None,
        observed_last=max(observed).isoformat() if observed else None,
        availability_policy="session_bounded_no_acquisition_pit", future_actions_applied=False,
        basis="as_traded", historical_identity_certified=False)
    _gap(session, run, member, domain, "historical_eligibility_unknown", None,
        official_calendar=False, listing_history=False, provisional=True)
    if count:
        _gap(session, run, member, domain, "provisional_missing_date", count,
            basis="provisional_weekdays", spans=missing, eligibility="unknown",
            includes_holidays_and_prelisting_dates=True)
    if not observed:
        _gap(session, run, member, domain, "missing_domain_observations", 1)
    if collector["unsupported_jobs"]:
        _gap(session, run, member, domain, "source_unsupported", collector["unsupported_jobs"],
            job_status_scope="collector_only", data_absence_not_implied=True)


def _period_insert(session, rows):
    if not rows:
        return
    table = CoverageFinancialPeriod
    statement = insert(table).values(rows)
    qualified_count = table.qualified_source_fact_count + statement.excluded.qualified_source_fact_count
    session.execute(statement.on_conflict_do_update(index_elements=[table.member_id, table.period_key],
        set_={"source_fact_count": table.source_fact_count + statement.excluded.source_fact_count,
            "qualified_source_fact_count": qualified_count,
            "document_count": table.document_count + statement.excluded.document_count,
            "qualified_document_count": table.qualified_document_count + statement.excluded.qualified_document_count,
            "status": case((qualified_count > 0, "public_pit_qualified_source_period"),
                else_="uncertified_public_pit_period")}))


def _financial_summary(session, run, member, security, collector):
    applicability = company_fundamentals_applicability(security.security_type)
    if applicability == "not_applicable":
        _summary(session, run, member, "fundamentals", applicability=applicability, collector=collector,
            company_financials_required=False, product_data_readiness="not_evaluated")
        return
    _gap(session, run, member, "fundamentals", "financial_period_expectation_unknown", 1,
        fiscal_schedule="not_inferred", missing_expected_period_count=None)
    if applicability == "unknown":
        _gap(session, run, member, "fundamentals", "fundamentals_applicability_unknown", 1)
    if collector["unsupported_jobs"]:
        _gap(session, run, member, "fundamentals", "source_unsupported", collector["unsupported_jobs"],
            job_status_scope="collector_only", data_absence_not_implied=True)
    if not security.issuer_id:
        _gap(session, run, member, "fundamentals", "missing_issuer", 1)
        _summary(session, run, member, "fundamentals", applicability=applicability, collector=collector,
            qualified_source_fact_count=0, expected_period_basis="unknown", missing_input=True)
        return
    docs = select(FinancialSourceDocument, Filing).join(Filing,
        FinancialSourceDocument.filing_id == Filing.id).where(Filing.issuer_id == security.issuer_id,
            FinancialSourceDocument.captured_at <= run.watermark, Filing.captured_at <= run.watermark)
    document_count, qualified_documents = 0, 0
    revision_counts, qualified_revisions = defaultdict(int), defaultdict(int)
    reasons = defaultdict(int)
    cutoff = market_cutoff(run.output_end, member.market)
    for document, filing in session.execute(docs).yield_per(10):
        evidence = list(session.scalars(select(FinancialPublicationEvidence).where(
            FinancialPublicationEvidence.document_id == document.id,
            FinancialPublicationEvidence.captured_at <= run.watermark,
            FinancialPublicationEvidence.recorded_at <= run.watermark)))
        resolution = resolve_document_availability(document, filing, evidence,
            history_mode="reconstructed_public", as_of=cutoff)
        qualified = resolution["status"] == "qualified" and document.extraction_complete
        reasons[resolution["reason"]] += 1
        # Aggregate actual contexts only. Invalid/unknown periods remain explicit
        # rows; no Gregorian quarter or year template is asserted as expected.
        fact = SourceFinancialFact
        period_end = func.coalesce(fact.instant, fact.period_end)
        # An uncertified document cannot contribute a clean fact. Avoid any
        # unnecessary per-fact decision/TOAST reads for that common case.
        accepted = (and_(_latest_status(fact, "source_financial_fact", run.watermark) == "accepted",
            fact.evidence["parse_status"].astext == "ok") if qualified else false())
        statement = select(fact.period_kind, fact.period_start, fact.period_end, fact.instant,
            func.count().label("count"), func.count().filter(accepted).label("accepted"))
        statement = statement.where(fact.document_id == document.id, fact.issuer_id == security.issuer_id,
            fact.captured_at <= run.watermark, fact.extracted_at <= run.watermark,
            or_(period_end.is_(None), and_(period_end >= run.output_start, period_end <= run.output_end)))
        statement = statement.group_by(fact.period_kind, fact.period_start, fact.period_end, fact.instant)
        rows, has_period = [], False
        for period in session.execute(statement).yield_per(100):
            has_period = True
            known = ((period.period_kind == "instant" and (period.instant or period.period_end) is not None)
                or (period.period_kind == "duration" and period.period_start is not None
                    and period.period_end is not None and period.period_start <= period.period_end))
            clean = period.accepted if qualified and known else 0
            rows.append({"id": new_id(), "run_id": run.id, "member_id": member.id,
                "period_key": stable_hash([period.period_kind, period.period_start, period.period_end, period.instant]),
                "period_kind": period.period_kind, "period_start": period.period_start,
                "period_end": period.period_end, "instant": period.instant,
                "source_fact_count": period.count, "qualified_source_fact_count": clean,
                "document_count": 1, "qualified_document_count": int(bool(clean)),
                "status": "public_pit_qualified_source_period" if clean else "uncertified_public_pit_period",
                "details": {"period_identity_known": known,
                    "qualification_scope": "accepted_source_numeric_structure_exact_original_public_pit",
                    "revision_policy": "exact_certified_version_including_corrections",
                    "canonical_metric_readiness": "not_evaluated", "expected_inputs": "unknown"}})
            if len(rows) == 100:
                _period_insert(session, rows)
                rows = []
        _period_insert(session, rows)
        document_count += int(has_period)
        qualified_documents += int(has_period and qualified)
        if has_period:
            revision_counts[document.revision_kind] += 1
            qualified_revisions[document.revision_kind] += int(qualified)
    table = CoverageFinancialPeriod
    counts = session.execute(select(func.count(), func.count().filter(table.qualified_source_fact_count > 0),
        func.coalesce(func.sum(table.source_fact_count), 0), func.coalesce(func.sum(table.qualified_source_fact_count), 0))
        .where(table.member_id == member.id)).one()
    if not counts[0]:
        _gap(session, run, member, "fundamentals", "missing_financial_inputs", 1,
            expected_periods="unknown", cannot_infer_filing_schedule=True)
    if counts[0] > counts[1]:
        _gap(session, run, member, "fundamentals", "uncertified_public_pit_period", counts[0] - counts[1],
            period_rows_table="coverage_financial_period", qualification_reasons=dict(reasons))
    _summary(session, run, member, "fundamentals", counts[0], counts[1], applicability=applicability,
        collector=collector, expected_period_basis="unknown", source_fact_count=counts[2],
        qualified_source_fact_count=counts[3], document_count=document_count,
        qualified_document_count=qualified_documents, period_rows_table="coverage_financial_period",
        document_revision_counts=dict(revision_counts),
        qualified_revision_counts={kind: count for kind, count in qualified_revisions.items() if count},
        revision_policy="exact_certified_version_including_corrections",
        clean_scope="accepted_source_numeric_structure_exact_original_public_pit",
        canonical_metric_readiness="not_evaluated", no_future_publications=True,
        historical_issuer_identity_certified=False, missing_input=not bool(counts[0]))


def advance_coverage_run(session, run_id, *, batch_size=25):
    """Audit a bounded batch atomically; caller commits the result and cursor."""
    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise ValueError("coverage batch size must be an integer from 1 to 100")
    run = session.scalar(select(CoverageRun).where(CoverageRun.id == run_id).with_for_update())
    if run is None:
        raise ValueError("coverage run not found")
    if run.policy_version != POLICY_VERSION:
        raise ValueError("unsupported coverage policy")
    if "supplemental_readiness" not in run.details:
        run.details = {**run.details, "supplemental_readiness": dict(SUPPLEMENTAL_READINESS)}
    if run.status == "audit_complete":
        return coverage_report(session, run_id)
    members = list(session.scalars(select(CoverageMember).where(CoverageMember.run_id == run.id,
        CoverageMember.ordinal > run.cursor).order_by(CoverageMember.ordinal).limit(batch_size)))
    ids = {member.security_id for member in members if member.security_id}
    securities = {value.id: value for value in session.scalars(select(Security).where(Security.id.in_(ids)))}
    prices, vwap = _dates(session, run, tuple(securities), "prices"), _dates(session, run, tuple(securities), "vwap")
    collectors = _collectors(session, securities)
    actions, products = _supplemental_counts(session, run, tuple(securities))
    expected = provisional_weekdays(run.output_start, run.output_end)
    for member in members:
        security = securities.get(member.security_id)
        _supplemental_readiness(session, run, member, security, actions, products)
        if security is None or security.market != member.market or (
                member.member.get("identifier") and member.member["identifier"] != security.source_identifier):
            _gap(session, run, member, "catalog", "missing_security" if security is None else "security_identity_mismatch", 1,
                original_member=member.member)
            for domain in ("prices", "vwap", "fundamentals"):
                _summary(session, run, member, domain, missing_security=True,
                    applicability="unknown", expected_period_basis="unknown")
        else:
            jobs = collectors.get(security.id, [])
            if security.issuer_id:
                jobs = jobs + collectors.get(security.issuer_id, [])
            _daily_summary(session, run, member, "prices", prices.get(security.id, {}),
                _collector_summary(jobs, "prices"), expected)
            _daily_summary(session, run, member, "vwap", vwap.get(security.id, {}),
                _collector_summary(jobs, "vwap"), expected)
            _financial_summary(session, run, member, security, _collector_summary(jobs, "fundamentals"))
        member.done = True
        run.cursor = member.ordinal
        run.processed_count += 1
    run.status = "audit_complete" if run.processed_count == run.target_count else "running"
    session.flush()
    return coverage_report(session, run_id)


def coverage_report(session, run_id):
    """Read compact SQL aggregates; detailed members/periods stay queryable in SQL."""
    run = session.get(CoverageRun, run_id)
    if run is None:
        raise ValueError("coverage run not found")
    markets = dict(session.execute(select(CoverageMember.market, func.count()).where(
        CoverageMember.run_id == run.id).group_by(CoverageMember.market)).all())
    expected = run.details["expected_market_counts"]
    groups = session.execute(select(CoverageSummary.market, CoverageSummary.domain, func.count().label("members"),
        func.sum(CoverageSummary.observed_count).label("observed"), func.sum(CoverageSummary.clean_count).label("clean"),
        func.sum(CoverageSummary.expected_count).label("expected"), func.sum(CoverageSummary.missing_count).label("missing"))
        .where(CoverageSummary.run_id == run.id).group_by(CoverageSummary.market, CoverageSummary.domain))
    values = [{"market": row.market, "domain": row.domain, "audited_members": row.members,
        "observed_count": row.observed, "clean_count": row.clean, "expected_count": row.expected,
        "missing_count": row.missing, "expected_basis": "unknown" if row.domain == "fundamentals" else "provisional_weekdays",
        "coverage_complete": False} for row in groups]
    gaps = dict(session.execute(select(CoverageGap.reason, func.sum(func.coalesce(CoverageGap.count, 1)))
        .where(CoverageGap.run_id == run.id).group_by(CoverageGap.reason)).all())
    return {"run_id": run.id, "manifest_id": run.manifest_id, "manifest_hash": run.manifest_hash,
        "policy_version": run.policy_version, "status": run.status, "target_count": run.target_count,
        "processed_count": run.processed_count, "cursor": run.cursor,
        "market_counts": markets, "expected_market_counts": expected,
        "denominator_matches_expected": markets == expected, "coverage_complete": False,
        "output_start": run.output_start.isoformat(), "output_end": run.output_end.isoformat(),
        "watermark": run.watermark.isoformat(), "domains": values, "gap_counts": gaps,
        "calendar_basis": "provisional_weekdays", "historical_eligibility": "unknown",
        "financial_period_expectations": "unknown", "collector_termination_certifies_coverage": False,
        "supplemental_readiness": run.details.get("supplemental_readiness", dict(SUPPLEMENTAL_READINESS))}


def materialize_coverage(engine, *, manifest_id=None, run_id=None, batch_size=25, max_batches=1, **kwargs):
    """Commit each bounded batch, allowing durable background/resumable execution."""
    if type(max_batches) is not int or not 1 <= max_batches <= 10000:
        raise ValueError("coverage requires a positive bounded batch budget")
    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise ValueError("coverage batch size must be an integer from 1 to 100")
    if run_id is not None and (manifest_id is not None or kwargs):
        raise ValueError("resume uses the original SQL run configuration")
    if run_id is None:
        if not manifest_id:
            raise ValueError("manifest_id is required for a new coverage run")
        with transaction(engine) as session:
            run_id = start_coverage_run(session, manifest_id, **kwargs)
    for _ in range(max_batches):
        with transaction(engine) as session:
            report = advance_coverage_run(session, run_id, batch_size=batch_size)
        if report["status"] == "audit_complete":
            break
    return report
