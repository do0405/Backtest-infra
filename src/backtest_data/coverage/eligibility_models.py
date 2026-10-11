"""Append-only evidence and bounded immutable coverage overlay pages."""
from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, new_id, utcnow


class CalendarEligibilityEvidence(Base):
    __tablename__ = "calendar_eligibility_evidence"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    market: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(80))
    calendar_version: Mapped[str] = mapped_column(String(80))
    start: Mapped[date] = mapped_column(Date)
    end: Mapped[date] = mapped_column(Date)
    rows: Mapped[list] = mapped_column(JSONB)
    complete_interval: Mapped[bool] = mapped_column(Boolean)
    authoritative: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(30))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    policy_version: Mapped[str] = mapped_column(String(80))
    assertion_hash: Mapped[str] = mapped_column(String(64), unique=True)
    source_refs: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        CheckConstraint('"start" <= "end"', name="ordered_interval"),
        CheckConstraint("recorded_at >= captured_at", name="capture_record_order"),
        CheckConstraint("status IN ('accepted','rejected')", name="status"),
        CheckConstraint("NOT complete_interval OR jsonb_array_length(rows) > 0", name="nonempty_complete"),
        Index("ix_calendar_eligibility_market_cutoff", "market", "recorded_at", "captured_at"),
    )


class ListingEligibilityEvidence(Base):
    __tablename__ = "listing_eligibility_evidence"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    security_id: Mapped[str] = mapped_column(ForeignKey("security.id"))
    market: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(80))
    listing_start: Mapped[date] = mapped_column(Date)
    listing_end: Mapped[date | None] = mapped_column(Date)
    scope_start: Mapped[date] = mapped_column(Date)
    scope_end: Mapped[date] = mapped_column(Date)
    complete_history: Mapped[bool] = mapped_column(Boolean)
    initial_listing_declared: Mapped[bool] = mapped_column(Boolean)
    lifecycle_ref: Mapped[str | None] = mapped_column(String(200))
    identity_status: Mapped[str] = mapped_column(String(40))
    grade: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    policy_version: Mapped[str] = mapped_column(String(80))
    assertion_hash: Mapped[str] = mapped_column(String(64), unique=True)
    source_refs: Mapped[dict] = mapped_column(JSONB)
    evidence: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        CheckConstraint("scope_start <= scope_end", name="ordered_scope"),
        CheckConstraint("listing_end IS NULL OR listing_start < listing_end", name="ordered_listing"),
        CheckConstraint("recorded_at >= captured_at", name="capture_record_order"),
        CheckConstraint("status IN ('accepted','rejected')", name="status"),
        CheckConstraint("grade IN ('verified','source_declared')", name="grade"),
        CheckConstraint("identity_status IN ('verified_security','source_declared_security','unresolved_symbol')", name="identity"),
        Index("ix_listing_eligibility_security_cutoff", "security_id", "recorded_at", "captured_at"),
    )


class EligibilityOverlay(Base):
    __tablename__ = "coverage_eligibility_overlay"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("coverage_run.id"))
    baseline_manifest_hash: Mapped[str] = mapped_column(String(64))
    baseline_watermark: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    baseline_target_count: Mapped[int] = mapped_column(Integer)
    output_start: Mapped[date] = mapped_column(Date)
    output_end: Mapped[date] = mapped_column(Date)
    evidence_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    profile: Mapped[str] = mapped_column(String(80))
    policy_version: Mapped[str] = mapped_column(String(80))
    snapshot_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("ix_eligibility_overlay_run_cutoff", "run_id", "evidence_cutoff"),)


class EligibilityOverlayPage(Base):
    __tablename__ = "coverage_eligibility_page"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    overlay_id: Mapped[str] = mapped_column(ForeignKey("coverage_eligibility_overlay.id"))
    start_cursor: Mapped[int] = mapped_column(Integer)
    end_cursor: Mapped[int] = mapped_column(Integer)
    member_limit: Mapped[int] = mapped_column(Integer)
    previous_hash: Mapped[str] = mapped_column(String(64))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    complete: Mapped[bool] = mapped_column(Boolean)
    members: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("overlay_id", "start_cursor"),
        CheckConstraint("start_cursor >= 0 AND end_cursor > start_cursor", name="ordered_cursor"),
        CheckConstraint("member_limit BETWEEN 1 AND 100 AND jsonb_array_length(members) <= member_limit", name="bounded_page"),
        Index("ix_eligibility_page_overlay_end", "overlay_id", "end_cursor"),
    )
