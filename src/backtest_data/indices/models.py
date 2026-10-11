from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ..db import Base, new_id

class IndexHistoryRecord(Base):
    __tablename__ = 'index_history_record'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey('supplemental_document.id'))
    record_kind: Mapped[str] = mapped_column(String(40))
    index_key: Mapped[str] = mapped_column(Text)
    row_key: Mapped[str] = mapped_column(Text)
    member_identifier: Mapped[str | None] = mapped_column(Text)
    member_name: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str | None] = mapped_column(String(20))
    announced_date: Mapped[date | None] = mapped_column(Date)
    published_date: Mapped[date | None] = mapped_column(Date)
    effective_date: Mapped[date | None] = mapped_column(Date)
    effective_phase: Mapped[str | None] = mapped_column(String(40))
    snapshot_date: Mapped[date | None] = mapped_column(Date)
    weight_date: Mapped[date | None] = mapped_column(Date)
    weight: Mapped[Decimal | None] = mapped_column(Numeric())
    weight_unit: Mapped[str | None] = mapped_column(String(30))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fields: Mapped[dict] = mapped_column(JSONB)
    issues: Mapped[list] = mapped_column(JSONB)
    canonical_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    signature: Mapped[str] = mapped_column(String(64), unique=True)
    __table_args__ = (
        CheckConstraint('canonical_eligible = false', name='source_stage_only'),
        CheckConstraint("record_kind IN ('snapshot_member','change','announcement','access_gap')", name='known_record_kind'),
        CheckConstraint("action IS NULL OR action IN ('add','delete')", name='known_action'),
        CheckConstraint("weight IS NULL OR (weight >= 0 AND weight_date IS NOT NULL AND weight_unit IN ('fraction','percent'))", name='dated_weight_unit'),
        Index('ix_index_history_index_dates', 'index_key', 'published_date', 'effective_date'),
        Index('ix_index_history_document_kind', 'document_id', 'record_kind'),
    )

class IndexAcquisitionState(Base):
    __tablename__ = 'index_acquisition_state'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    manifest_id: Mapped[str] = mapped_column(ForeignKey('target_universe_manifest.id'))
    job_key: Mapped[str] = mapped_column(String(80))
    target_hash: Mapped[str] = mapped_column(String(64))
    target_count: Mapped[int] = mapped_column(Integer)
    output_start: Mapped[date] = mapped_column(Date)
    output_end: Mapped[date] = mapped_column(Date)
    cursor: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('manifest_id', 'job_key', name='uq_index_acquisition_manifest_job'),)
