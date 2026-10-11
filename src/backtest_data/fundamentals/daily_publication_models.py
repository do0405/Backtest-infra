"""Independent immutable daily metadata assertions; strict certificates stay separate."""
from datetime import date, datetime
from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ..db import Base, new_id, utcnow


class FinancialDailyPublicationEvidence(Base):
    __tablename__ = "financial_daily_publication_evidence"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("financial_source_document.id"))
    source_fact_id: Mapped[str | None] = mapped_column(ForeignKey("source_financial_fact.id"))
    filing_id: Mapped[str] = mapped_column(ForeignKey("filing.id"))
    issuer_id: Mapped[str] = mapped_column(ForeignKey("issuer.id"))
    source: Mapped[str] = mapped_column(String(80))
    market: Mapped[str] = mapped_column(String(10))
    source_version_ref: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str | None] = mapped_column(String(200))
    public_day: Mapped[date | None] = mapped_column(Date)
    effective_available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_grade: Mapped[str] = mapped_column(String(40))
    metadata_date_basis: Mapped[str] = mapped_column(String(80))
    calendar_basis: Mapped[str] = mapped_column(String(120))
    policy_version: Mapped[str] = mapped_column(String(80))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    assertion_hash: Mapped[str] = mapped_column(String(64), unique=True)
    evidence: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        Index("ix_daily_publication_document_policy", "document_id", "policy_version"),
        Index("ix_daily_publication_source_fact", "source_fact_id"),
        CheckConstraint("status IN ('qualified','unavailable')", name="daily_status"),
        CheckConstraint("evidence_grade = 'metadata_grade'", name="metadata_grade"),
        CheckConstraint("recorded_at >= captured_at", name="daily_capture_record_order"),
        CheckConstraint("status != 'qualified' OR (public_day IS NOT NULL AND effective_available_at IS NOT NULL)", name="daily_public_bound"),
    )
