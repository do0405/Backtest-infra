from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, new_id, utcnow


class SelectionDecision(Base):
    __tablename__ = "selection_decision"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    record_domain: Mapped[str] = mapped_column(String(80), index=True)
    record_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(40))
    rule_version: Mapped[str] = mapped_column(String(120))
    evidence_grade: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(String(1000))
    actor: Mapped[str] = mapped_column(String(120))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DataSegment(Base):
    __tablename__ = "data_segment"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    domain: Mapped[str] = mapped_column(String(80))
    partition_key: Mapped[str] = mapped_column(String(200))
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SegmentMember(Base):
    __tablename__ = "segment_member"
    segment_id: Mapped[str] = mapped_column(ForeignKey("data_segment.id"), primary_key=True)
    record_domain: Mapped[str] = mapped_column(String(80), primary_key=True)
    record_id: Mapped[str] = mapped_column(String(36), primary_key=True)


class DatasetRelease(Base):
    __tablename__ = "dataset_release"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    manifest: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(40))
    sealed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReleaseSegment(Base):
    __tablename__ = "release_segment"
    release_id: Mapped[str] = mapped_column(ForeignKey("dataset_release.id"), primary_key=True)
    segment_id: Mapped[str] = mapped_column(ForeignKey("data_segment.id"), primary_key=True)


class ReleasePointer(Base):
    __tablename__ = "release_pointer"
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    release_id: Mapped[str] = mapped_column(ForeignKey("dataset_release.id"))


class ValidationIssue(Base):
    __tablename__ = "validation_issue"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    record_domain: Mapped[str] = mapped_column(String(80))
    record_id: Mapped[str | None] = mapped_column(String(36))
    rule: Mapped[str] = mapped_column(String(120))
    severity: Mapped[str] = mapped_column(String(20))
    evidence: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
