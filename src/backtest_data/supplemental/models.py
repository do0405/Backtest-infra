from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ..db import Base, new_id

class SupplementalAPICall(Base):
    __tablename__ = 'supplemental_api_call'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(80))
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index('ix_supplemental_api_call_source_time', 'source', 'attempted_at'),)

class SupplementalDocument(Base):
    __tablename__='supplemental_document'
    id: Mapped[str]=mapped_column(String(36),primary_key=True,default=new_id)
    source_contract_id: Mapped[str]=mapped_column(ForeignKey('source_contract.id'))
    raw_id: Mapped[str]=mapped_column(ForeignKey('raw_object.id'))
    source: Mapped[str]=mapped_column(String(80))
    domain: Mapped[str]=mapped_column(String(80))
    source_url: Mapped[str]=mapped_column(Text)
    content_hash: Mapped[str]=mapped_column(String(64))
    signature: Mapped[str]=mapped_column(String(64),unique=True)
    captured_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))
    request_scope: Mapped[dict]=mapped_column(JSONB)
    evidence: Mapped[dict]=mapped_column(JSONB)
    __table_args__=(Index('ix_supplemental_document_source_domain','source','domain'),)

class SupplementalObservation(Base):
    __tablename__='supplemental_observation'
    id: Mapped[str]=mapped_column(String(36),primary_key=True,default=new_id)
    document_id: Mapped[str]=mapped_column(ForeignKey('supplemental_document.id'))
    record_kind: Mapped[str]=mapped_column(String(40))
    entity_key: Mapped[str]=mapped_column(Text)
    row_key: Mapped[str]=mapped_column(Text)
    entity_name: Mapped[str|None]=mapped_column(Text)
    member_identifier: Mapped[str|None]=mapped_column(Text)
    member_name: Mapped[str|None]=mapped_column(Text)
    product_type: Mapped[str|None]=mapped_column(String(80))
    currency: Mapped[str|None]=mapped_column(String(30))
    report_date: Mapped[date|None]=mapped_column(Date)
    filing_date: Mapped[date|None]=mapped_column(Date)
    published_date: Mapped[date|None]=mapped_column(Date)
    announced_date: Mapped[date|None]=mapped_column(Date)
    effective_date: Mapped[date|None]=mapped_column(Date)
    ex_date: Mapped[date|None]=mapped_column(Date)
    record_date: Mapped[date|None]=mapped_column(Date)
    pay_date: Mapped[date|None]=mapped_column(Date)
    maturity_date: Mapped[date|None]=mapped_column(Date)
    ipo_date: Mapped[date|None]=mapped_column(Date)
    delisting_date: Mapped[date|None]=mapped_column(Date)
    published_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    event_timestamp: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    quantity: Mapped[Decimal|None]=mapped_column(Numeric())
    value: Mapped[Decimal|None]=mapped_column(Numeric())
    weight: Mapped[Decimal|None]=mapped_column(Numeric())
    nav: Mapped[Decimal|None]=mapped_column(Numeric())
    net_assets: Mapped[Decimal|None]=mapped_column(Numeric())
    total_assets: Mapped[Decimal|None]=mapped_column(Numeric())
    total_liabilities: Mapped[Decimal|None]=mapped_column(Numeric())
    fee_rate: Mapped[Decimal|None]=mapped_column(Numeric())
    price: Mapped[Decimal|None]=mapped_column(Numeric())
    sum_price_volume: Mapped[Decimal|None]=mapped_column(Numeric())
    eligible_volume: Mapped[Decimal|None]=mapped_column(Numeric())
    captured_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))
    date_basis: Mapped[str]=mapped_column(String(120))
    fields: Mapped[dict]=mapped_column(JSONB)
    issues: Mapped[list]=mapped_column(JSONB)
    canonical_eligible: Mapped[bool]=mapped_column(Boolean,default=False)
    signature: Mapped[str]=mapped_column(String(64),unique=True)
    __table_args__=(CheckConstraint('canonical_eligible = false',name='source_stage_only'),
        Index('ix_supplemental_observation_doc_kind','document_id','record_kind'))
