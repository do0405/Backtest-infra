from sqlalchemy import func, select

from ..catalog.models import IdentifierHistory, Issuer, Security, SourceQualification
from ..credentials import load_credentials
from ..db import transaction, utcnow
from ..fundamentals.models import Filing, SourceFinancialFact, FundamentalFact, DerivedFact
from ..ingestion.models import CollectionJob, TargetUniverseManifest
from ..prices.models import PriceObservation, VWAPDailyStat
from ..releases.models import DatasetRelease, ValidationIssue


def operating_status(engine, market="both"):
    counts = {}
    with transaction(engine) as session:
        for model in (Issuer, Security, IdentifierHistory, Filing, SourceFinancialFact, FundamentalFact, DerivedFact, PriceObservation, VWAPDailyStat, DatasetRelease, ValidationIssue):
            counts[model.__tablename__] = session.scalar(select(func.count()).select_from(model))
        jobs = session.execute(select(CollectionJob.source, CollectionJob.domain, CollectionJob.status, func.count())
                               .group_by(CollectionJob.source, CollectionJob.domain, CollectionJob.status)).all()
        manifest = session.scalar(select(TargetUniverseManifest).order_by(TargetUniverseManifest.captured_at.desc()).limit(1))
        scope = {"manifest_id": manifest.id, "scope": manifest.scope, "target_entries": len(manifest.members), "gaps": manifest.gaps} if manifest else None
    credentials = load_credentials(sources=["kis", "opendart", "sec", "alpaca"])
    return {"operational_complete": False, "domain_coverage_complete": False, "reported_at": utcnow().isoformat(),
            "market_requested": market, "count_scope": "all_persisted_records", "counts": counts,
            "jobs": [{"source": row[0], "domain": row[1], "status": row[2], "count": row[3]} for row in jobs],
            "latest_target": scope, "credentials_configured": {"opendart": bool(credentials.opendart_api_key),
                "kis": bool(credentials.kis_app_key and credentials.kis_app_secret), "sec_identification": bool(credentials.sec_user_agent),
                "alpaca": bool(credentials.alpaca_key and credentials.alpaca_secret)}}
