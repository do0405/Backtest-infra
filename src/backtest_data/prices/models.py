from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, new_id


class PriceObservation(Base):
    __tablename__ = "price_observation"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    security_id: Mapped[str] = mapped_column(ForeignKey("security.id"), index=True)
    source_contract_id: Mapped[str] = mapped_column(ForeignKey("source_contract.id"))
    raw_id: Mapped[str] = mapped_column(ForeignKey("raw_object.id"))
    session: Mapped[date] = mapped_column(Date, index=True)
    basis: Mapped[str] = mapped_column(String(40))
    feed: Mapped[str] = mapped_column(String(80))
    venue: Mapped[str] = mapped_column(String(80))
    trade_scope: Mapped[str] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(10))
    quantity_unit: Mapped[str] = mapped_column(String(40))
    open: Mapped[Decimal] = mapped_column(Numeric())
    high: Mapped[Decimal] = mapped_column(Numeric())
    low: Mapped[Decimal] = mapped_column(Numeric())
    close: Mapped[Decimal] = mapped_column(Numeric())
    volume: Mapped[Decimal | None] = mapped_column(Numeric())
    notional: Mapped[Decimal | None] = mapped_column(Numeric())
    signature: Mapped[str] = mapped_column(String(64), unique=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict] = mapped_column(JSONB)


class CorporateActionObservation(Base):
    __tablename__ = "corporate_action_observation"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    security_id: Mapped[str] = mapped_column(ForeignKey("security.id"), index=True)
    event_id: Mapped[str] = mapped_column(String(150))
    action_type: Mapped[str] = mapped_column(String(80))
    effective_session: Mapped[date] = mapped_column(Date)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    terms: Mapped[dict] = mapped_column(JSONB)
    evidence: Mapped[dict] = mapped_column(JSONB)


class VWAPDailyStat(Base):
    __tablename__ = "vwap_daily_stat"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    security_id: Mapped[str] = mapped_column(ForeignKey("security.id"), index=True)
    source_contract_id: Mapped[str] = mapped_column(ForeignKey("source_contract.id"))
    raw_id: Mapped[str] = mapped_column(ForeignKey("raw_object.id"))
    session: Mapped[date] = mapped_column(Date)
    sum_price_volume: Mapped[Decimal] = mapped_column(Numeric())
    eligible_volume: Mapped[Decimal] = mapped_column(Numeric())
    trade_scope: Mapped[str] = mapped_column(String(120))
    method: Mapped[str] = mapped_column(String(80))
    basis: Mapped[str] = mapped_column(String(80))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    materialized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict] = mapped_column(JSONB)
