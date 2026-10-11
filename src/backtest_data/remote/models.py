from datetime import datetime
from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ..db import Base


class RemoteImportReceipt(Base):
    __tablename__ = "remote_import_receipt"
    bundle_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    stream_id: Mapped[str] = mapped_column(String(80))
    sequence: Mapped[int] = mapped_column(Integer)
    manifest_hash: Mapped[str] = mapped_column(String(64))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    inserted_rows: Mapped[int] = mapped_column(Integer)
    scope: Mapped[dict] = mapped_column(JSONB)
    checkpoint: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("stream_id", "sequence"),)


class RemoteStreamCursor(Base):
    __tablename__ = "remote_stream_cursor"
    stream_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    committed_sequence: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
