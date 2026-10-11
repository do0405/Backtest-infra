"""Operational trade-page checkpoints and deduplication; source bytes are immutable."""
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ..db import Base, new_id, utcnow


class VWAPTradeRun(Base):
    __tablename__ = 'vwap_trade_run'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    security_id: Mapped[str] = mapped_column(ForeignKey('security.id'))
    symbol: Mapped[str] = mapped_column(String(120))
    session: Mapped[date] = mapped_column(Date)
    rule: Mapped[str] = mapped_column(String(80))
    request_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(40))
    cursor: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VWAPTradeEvent(Base):
    __tablename__ = 'vwap_trade_event'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey('vwap_trade_run.id'), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey('supplemental_document.id'))
    event_key: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    timestamp_second: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    timestamp_nanosecond: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Numeric())
    quantity: Mapped[Decimal] = mapped_column(Numeric())
    numerator: Mapped[Decimal] = mapped_column(Numeric())
    eligible: Mapped[bool] = mapped_column(Boolean)
    __table_args__ = (UniqueConstraint('run_id', 'event_key', name='uq_vwap_trade_run_event'),)
