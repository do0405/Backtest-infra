"""Operational admission state, kept separate from source evidence."""
from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base


class ProviderQuotaPolicy(Base):
    __tablename__ = 'provider_quota_policy'
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    minimum_interval_seconds: Mapped[float] = mapped_column(Float)
    window_kind: Mapped[str] = mapped_column(String(20))
    credit_limit: Mapped[int | None] = mapped_column(Integer)
    timezone: Mapped[str] = mapped_column(String(80))
    require_allocation: Mapped[bool] = mapped_column(Boolean, server_default=text('false'))
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint('minimum_interval_seconds > 0', name='positive_interval'),
        CheckConstraint("window_kind IN ('none','calendar_day','rolling_24h')", name='window'),
        CheckConstraint("(window_kind = 'none' AND credit_limit IS NULL) OR (window_kind <> 'none' AND credit_limit > 0)", name='limit'),
        CheckConstraint("NOT require_allocation OR window_kind = 'calendar_day'", name='allocation_window'),
    )


class ProviderQuotaReservation(Base):
    __tablename__ = 'provider_quota_reservation'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, server_default=text('gen_random_uuid()::text'))
    key: Mapped[str] = mapped_column(ForeignKey('provider_quota_policy.key'))
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index('ix_provider_quota_reservation_key_clock', 'key', 'reserved_at'),)


class ProviderQuotaAllocationDay(Base):
    __tablename__ = 'provider_quota_allocation_day'
    key: Mapped[str] = mapped_column(ForeignKey('provider_quota_policy.key'), primary_key=True)
    allocation_date: Mapped[date] = mapped_column(Date, primary_key=True)
    credit_limit: Mapped[int] = mapped_column(Integer)
    binding_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (CheckConstraint('credit_limit >= 0', name='nonnegative_limit'),)
