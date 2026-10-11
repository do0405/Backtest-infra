"""Immutable KRX source rows, before units and dated identity qualification."""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, new_id


class KRXQuoteObservation(Base):
    __tablename__ = "krx_quote_observation"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_contract_id: Mapped[str] = mapped_column(ForeignKey("source_contract.id"))
    raw_id: Mapped[str] = mapped_column(ForeignKey("raw_object.id"))
    response_capture_id: Mapped[str] = mapped_column(ForeignKey("ingestion_attempt.id"))
    service: Mapped[str] = mapped_column(String(40))
    record_kind: Mapped[str] = mapped_column(String(40))
    request_date: Mapped[date] = mapped_column(Date)
    reported_date: Mapped[date | None] = mapped_column(Date)
    source_code: Mapped[str | None] = mapped_column(Text)
    source_name: Mapped[str | None] = mapped_column(Text)
    source_isin: Mapped[str | None] = mapped_column(Text)
    source_english_name: Mapped[str | None] = mapped_column(Text)
    index_class: Mapped[str | None] = mapped_column(Text)
    listing_date: Mapped[date | None] = mapped_column(Date)
    row_index: Mapped[int] = mapped_column(Integer)
    open: Mapped[Decimal | None] = mapped_column(Numeric())
    high: Mapped[Decimal | None] = mapped_column(Numeric())
    low: Mapped[Decimal | None] = mapped_column(Numeric())
    close: Mapped[Decimal | None] = mapped_column(Numeric())
    volume: Mapped[Decimal | None] = mapped_column(Numeric())
    notional: Mapped[Decimal | None] = mapped_column(Numeric())
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric())
    listed_shares: Mapped[Decimal | None] = mapped_column(Numeric())
    price_change: Mapped[Decimal | None] = mapped_column(Numeric())
    change_rate: Mapped[Decimal | None] = mapped_column(Numeric())
    nav: Mapped[Decimal | None] = mapped_column(Numeric())
    net_asset_total: Mapped[Decimal | None] = mapped_column(Numeric())
    indicative_value: Mapped[Decimal | None] = mapped_column(Numeric())
    indicative_value_total: Mapped[Decimal | None] = mapped_column(Numeric())
    underlying_index: Mapped[Decimal | None] = mapped_column(Numeric())
    index_change: Mapped[Decimal | None] = mapped_column(Numeric())
    index_change_rate: Mapped[Decimal | None] = mapped_column(Numeric())
    par_value: Mapped[Decimal | None] = mapped_column(Numeric())
    status: Mapped[str] = mapped_column(String(30))
    raw_fields: Mapped[dict] = mapped_column(JSONB)
    issues: Mapped[list] = mapped_column(JSONB)
    signature: Mapped[str] = mapped_column(String(64), unique=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    coverage_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        CheckConstraint("row_index >= 0", name="nonnegative_row_index"),
        CheckConstraint("coverage_complete = false", name="coverage_unqualified"),
        CheckConstraint("record_kind IN ('daily_quote', 'basic_information', 'index_quote')", name="record_kind"),
        CheckConstraint("status IN ('source_typed', 'source_issues')", name="source_status"),
        Index("ix_krx_quote_service_date_capture", "service", "request_date", "captured_at"),
        Index("ix_krx_quote_response_capture", "response_capture_id"),
    )
