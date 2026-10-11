"""Append-only certificates for the public availability of one exact document."""
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, new_id, utcnow


class FinancialPublicationEvidence(Base):
    __tablename__ = "financial_publication_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("financial_source_document.id"), index=True)
    filing_id: Mapped[str] = mapped_column(ForeignKey("filing.id"), index=True)
    issuer_id: Mapped[str] = mapped_column(ForeignKey("issuer.id"), index=True)
    source: Mapped[str] = mapped_column(String(80))
    accession: Mapped[str] = mapped_column(String(100))
    document_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(40))
    public_day: Mapped[date | None] = mapped_column(Date)
    effective_available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    precision: Mapped[str] = mapped_column(String(40))
    timezone: Mapped[str | None] = mapped_column(String(80))
    policy_version: Mapped[str] = mapped_column(String(80))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    assertion_hash: Mapped[str] = mapped_column(String(64))
    supersedes_assertion_hash: Mapped[str | None] = mapped_column(
        ForeignKey("financial_publication_evidence.assertion_hash"))
    evidence: Mapped[dict] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint("assertion_hash"),
        CheckConstraint("status IN ('qualified', 'unavailable', 'rejected', 'withdrawal')", name="status"),
        CheckConstraint("recorded_at >= captured_at", name="capture_record_order"),
        CheckConstraint("status NOT IN ('qualified', 'withdrawal') OR "
            "(public_day IS NOT NULL AND effective_available_at IS NOT NULL AND precision = 'public_day' "
            "AND timezone IS NOT NULL)", name="public_bound"),
        CheckConstraint("status NOT IN ('rejected', 'withdrawal') OR supersedes_assertion_hash IS NOT NULL",
            name="event_target"),
    )
