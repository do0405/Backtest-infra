from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, new_id, utcnow


class SourceContract(Base):
    __tablename__ = "source_contract"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(80))
    version: Mapped[str] = mapped_column(String(80))
    domain: Mapped[str] = mapped_column(String(80))
    market: Mapped[str] = mapped_column(String(10))
    specification: Mapped[dict] = mapped_column(JSONB)
    qualification: Mapped[str] = mapped_column(String(30), default="pending")
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("source", "version", "domain", "market"),)


class Issuer(Base):
    __tablename__ = "issuer"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    market: Mapped[str] = mapped_column(String(10))
    source_identifier: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(500))
    __table_args__ = (UniqueConstraint("market", "source_identifier"),)


class Security(Base):
    __tablename__ = "security"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    issuer_id: Mapped[str | None] = mapped_column(ForeignKey("issuer.id"))
    market: Mapped[str] = mapped_column(String(10))
    source_identifier: Mapped[str] = mapped_column(String(120))
    security_type: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(500))
    __table_args__ = (UniqueConstraint("market", "source_identifier"),)


class IdentifierHistory(Base):
    __tablename__ = "identifier_history"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    security_id: Mapped[str] = mapped_column(ForeignKey("security.id"))
    identifier_type: Mapped[str] = mapped_column(String(40))
    identifier: Mapped[str] = mapped_column(String(500), index=True)
    venue: Mapped[str] = mapped_column(String(80))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    evidence: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        Index("ix_identifier_history_alias_normalized_key", func.md5(evidence["normalized_key"].astext)),
        Index("ix_identifier_history_alias_assertion_key", evidence["assertion_key"].astext),
    )


class ExchangeSession(Base):
    __tablename__ = "exchange_session"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    market: Mapped[str] = mapped_column(String(10))
    session: Mapped[date] = mapped_column(Date)
    calendar_version: Mapped[str] = mapped_column(String(80))
    open_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    close_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("market", "session", "calendar_version"),)


class CatalogObservation(Base):
    __tablename__ = "catalog_observation"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(80))
    identifier: Mapped[str] = mapped_column(String(120), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    fields: Mapped[dict] = mapped_column(JSONB)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("source", "identifier", "content_hash"),)


class SourceQualification(Base):
    __tablename__ = "source_qualification"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(80), index=True)
    domain: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(40))
    evidence: Mapped[dict] = mapped_column(JSONB)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
