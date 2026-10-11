from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, new_id, utcnow


class Filing(Base):
    __tablename__ = "filing"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    issuer_id: Mapped[str] = mapped_column(ForeignKey("issuer.id"), index=True)
    source: Mapped[str] = mapped_column(String(80))
    accession: Mapped[str] = mapped_column(String(100))
    form: Mapped[str] = mapped_column(String(80))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    evidence: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("source", "accession", "issuer_id"),)


class FinancialSourceDocument(Base):
    __tablename__ = "financial_source_document"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    filing_id: Mapped[str] = mapped_column(ForeignKey("filing.id"))
    content_hash: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str] = mapped_column(String(1500))
    instance_key: Mapped[str] = mapped_column(String(500), default="")
    revision_kind: Mapped[str] = mapped_column(String(40), default="original")
    disclosure_profile: Mapped[str] = mapped_column(String(80), default="periodic_report")
    accounting_basis: Mapped[str] = mapped_column(String(80), default="unknown")
    assurance: Mapped[str] = mapped_column(String(80), default="unknown")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    extraction_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    inventory: Mapped[dict] = mapped_column(JSONB)
    parser_version: Mapped[str] = mapped_column(String(80))
    __table_args__ = (UniqueConstraint("filing_id", "content_hash", "parser_version", "instance_key"),)


class SourceFinancialFact(Base):
    __tablename__ = "source_financial_fact"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("financial_source_document.id"), index=True)
    issuer_id: Mapped[str] = mapped_column(ForeignKey("issuer.id"), index=True)
    concept: Mapped[str] = mapped_column(Text, index=True)
    account_name: Mapped[str] = mapped_column(Text)
    context_id: Mapped[str] = mapped_column(Text)
    dimensions: Mapped[dict] = mapped_column(JSONB)
    period_kind: Mapped[str] = mapped_column(String(40))
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    instant: Mapped[date | None] = mapped_column(Date)
    scope: Mapped[str] = mapped_column(String(80))
    unit: Mapped[str] = mapped_column(Text)
    original_unit: Mapped[str] = mapped_column(Text)
    value: Mapped[Decimal | None] = mapped_column(Numeric())
    raw_value: Mapped[str | None] = mapped_column(Text)
    is_nil: Mapped[bool] = mapped_column(Boolean, default=False)
    scale: Mapped[int | None]
    decimals: Mapped[str | None] = mapped_column(String(40))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    signature: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("document_id", "signature"),)


class FundamentalFact(Base):
    __tablename__ = "fundamental_fact"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    issuer_id: Mapped[str] = mapped_column(ForeignKey("issuer.id"), index=True)
    source_fact_id: Mapped[str] = mapped_column(ForeignKey("source_financial_fact.id"))
    metric_id: Mapped[str] = mapped_column(String(120), index=True)
    mapping_version: Mapped[str] = mapped_column(String(80))
    value: Mapped[Decimal | None] = mapped_column(Numeric())
    unit: Mapped[str] = mapped_column(String(120))
    period: Mapped[dict] = mapped_column(JSONB)
    basis: Mapped[dict] = mapped_column(JSONB)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    materialized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DerivedFact(Base):
    __tablename__ = "derived_fact"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    entity_id: Mapped[str] = mapped_column(String(120), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    metric_id: Mapped[str] = mapped_column(String(120), index=True)
    definition_version: Mapped[str] = mapped_column(String(120))
    definition_hash: Mapped[str] = mapped_column(String(64))
    signature: Mapped[str] = mapped_column(String(64), unique=True)
    value: Mapped[Decimal | None] = mapped_column(Numeric())
    unit: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str | None] = mapped_column(String(300))
    period: Mapped[dict] = mapped_column(JSONB)
    basis: Mapped[dict] = mapped_column(JSONB)
    input_refs: Mapped[list] = mapped_column(JSONB)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    materialized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evaluation_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FundProductFact(Base):
    __tablename__ = "fund_product_fact"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    security_id: Mapped[str] = mapped_column(ForeignKey("security.id"), index=True)
    source_contract_id: Mapped[str] = mapped_column(ForeignKey("source_contract.id"))
    metric_id: Mapped[str] = mapped_column(String(120))
    value: Mapped[Decimal | None] = mapped_column(Numeric())
    text_value: Mapped[str | None] = mapped_column(String(1500))
    unit: Mapped[str] = mapped_column(String(120))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict] = mapped_column(JSONB)
