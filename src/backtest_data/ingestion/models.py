from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, new_id, utcnow


class RawObject(Base):
    __tablename__ = "raw_object"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    storage_key: Mapped[str] = mapped_column(String(200), unique=True)
    byte_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IngestionAttempt(Base):
    __tablename__ = "ingestion_attempt"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_contract_id: Mapped[str] = mapped_column(ForeignKey("source_contract.id"))
    request_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30))
    evidence: Mapped[dict] = mapped_column(JSONB)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CollectionJob(Base):
    __tablename__ = "collection_job"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    identity_hash: Mapped[str] = mapped_column(String(64), unique=True)
    source: Mapped[str] = mapped_column(String(80))
    domain: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str] = mapped_column(String(120))
    request: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    checkpoint: Mapped[dict] = mapped_column(JSONB, default=dict)
    lease_owner: Mapped[str | None] = mapped_column(String(80))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fencing_token: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[int] = mapped_column(Integer, default=10)
    last_error: Mapped[str | None] = mapped_column(String(300))


class RateBudget(Base):
    __tablename__ = "rate_budget"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    next_available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TargetUniverseManifest(Base):
    __tablename__ = "target_universe_manifest"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scope: Mapped[dict] = mapped_column(JSONB)
    members: Mapped[list] = mapped_column(JSONB)
    gaps: Mapped[list] = mapped_column(JSONB)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
