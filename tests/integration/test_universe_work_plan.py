import pytest
from sqlalchemy import select, func

from backtest_data.catalog.universe import UniverseEntry
from backtest_data.catalog.collection import save_universe
from backtest_data.ingestion.models import CollectionJob, TargetUniverseManifest


pytestmark = pytest.mark.integration


def test_all_securities_remain_in_jobs_and_unresolved_history_is_explicit(db_session):
    entries = [UniverseEntry("KR", "KOSPI", f"{i:06d}", f"KR{i:010d}", f"Security{i}", "unclassified", {}) for i in range(150)]
    manifest = save_universe(db_session, entries, output_start="2016-01-01", output_end="2026-10-07",
                            evidence=[], gaps=[{"domain": "historical_universe", "reason": "source_unavailable"}])
    assert len(manifest.members) == 150
    assert manifest.scope["historical_universe_complete"] is False
    assert db_session.scalar(select(func.count()).select_from(CollectionJob)) == 450
    again = save_universe(db_session, entries, output_start="2016-01-01", output_end="2026-10-07", evidence=[], gaps=manifest.gaps)
    assert again.id == manifest.id
