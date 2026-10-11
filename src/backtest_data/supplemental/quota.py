"""Persist attempted free requests before network I/O, including failures."""
from datetime import timedelta
from sqlalchemy import select, func, text
from .models import SupplementalAPICall
from ..ingestion.http import ProviderError

def reserve_alpha_call(session, now):
    if now.tzinfo is None: raise ValueError('Aware quota clock required')
    session.execute(text("SELECT pg_advisory_xact_lock(71302907)"))
    used = session.scalar(select(func.count()).select_from(SupplementalAPICall).where(
        SupplementalAPICall.source == 'alpha_vantage',
        SupplementalAPICall.attempted_at > now - timedelta(days=1)))
    if used >= 25: raise ProviderError('alpha_vantage', 'free_rolling_daily_quota_deferred')
    session.add(SupplementalAPICall(source='alpha_vantage', attempted_at=now))
    session.flush()
