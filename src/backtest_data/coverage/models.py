"""Additive audit persistence; original evidence tables remain immutable."""
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, new_id, utcnow
from ..releases.models import SelectionDecision, ValidationIssue
from ..ingestion.models import CollectionJob


Index("ix_selection_decision_price_coverage_latest", SelectionDecision.record_id,
    SelectionDecision.recorded_at.desc(), SelectionDecision.id.desc(),
    postgresql_include=("status", "rule_version", "evidence_grade"),
    postgresql_where=text("record_domain = 'price_observation'"))
Index("ix_collection_job_coverage_entity", CollectionJob.entity_id)
Index("ix_validation_issue_financial_completion_lookup", ValidationIssue.record_domain,
    ValidationIssue.record_id, ValidationIssue.rule, ValidationIssue.created_at.desc(), ValidationIssue.id.desc())


class CoverageRun(Base):
    __tablename__ = "coverage_run"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    manifest_id: Mapped[str] = mapped_column(ForeignKey("target_universe_manifest.id"))
    manifest_hash: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(80))
    output_start: Mapped[date] = mapped_column(Date)
    output_end: Mapped[date] = mapped_column(Date)
    watermark: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(40), default="pending")
    target_count: Mapped[int] = mapped_column(Integer)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[dict] = mapped_column(JSONB)


class CoverageMember(Base):
    __tablename__ = "coverage_member"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("coverage_run.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    security_id: Mapped[str | None] = mapped_column(String(36), index=True)
    market: Mapped[str] = mapped_column(String(10))
    member: Mapped[dict] = mapped_column(JSONB)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("run_id", "ordinal"),)


class CoverageSummary(Base):
    __tablename__ = "coverage_summary"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("coverage_run.id"), index=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("coverage_member.id"), index=True)
    market: Mapped[str] = mapped_column(String(10))
    domain: Mapped[str] = mapped_column(String(40))
    observed_count: Mapped[int] = mapped_column(Integer)
    clean_count: Mapped[int] = mapped_column(Integer)
    expected_count: Mapped[int | None] = mapped_column(Integer)
    missing_count: Mapped[int | None] = mapped_column(Integer)
    coverage_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    details: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("member_id", "domain"),)


class CoverageGap(Base):
    __tablename__ = "coverage_gap"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("coverage_run.id"), index=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("coverage_member.id"), index=True)
    market: Mapped[str] = mapped_column(String(10))
    domain: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(String(120), index=True)
    count: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict] = mapped_column(JSONB)


class CoverageFinancialPeriod(Base):
    __tablename__ = "coverage_financial_period"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("coverage_run.id"), index=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("coverage_member.id"), index=True)
    period_key: Mapped[str] = mapped_column(String(64))
    period_kind: Mapped[str] = mapped_column(String(40))
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    instant: Mapped[date | None] = mapped_column(Date)
    source_fact_count: Mapped[int] = mapped_column(Integer)
    qualified_source_fact_count: Mapped[int] = mapped_column(Integer)
    document_count: Mapped[int] = mapped_column(Integer)
    qualified_document_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(80))
    details: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("member_id", "period_key"),)
