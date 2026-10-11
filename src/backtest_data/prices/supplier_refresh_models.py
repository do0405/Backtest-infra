"""Independent rolling update windows, leaving active backfill state untouched."""
from datetime import date,datetime
from sqlalchemy import Date,DateTime,ForeignKey,String,Integer,UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped,mapped_column
from ..db import Base,new_id,utcnow
class SupplierPriceRefreshState(Base):
    __tablename__='supplier_price_refresh_state'
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=new_id)
    manifest_id:Mapped[str]=mapped_column(ForeignKey('target_universe_manifest.id'))
    source:Mapped[str]=mapped_column(String(80))
    adjustment:Mapped[str]=mapped_column(String(80))
    target_hash:Mapped[str]=mapped_column(String(64))
    target_count:Mapped[int]=mapped_column(Integer)
    market_target_count:Mapped[int]=mapped_column(Integer)
    output_start:Mapped[date]=mapped_column(Date)
    output_end:Mapped[date]=mapped_column(Date)
    status:Mapped[str]=mapped_column(String(40))
    cursor:Mapped[dict]=mapped_column(JSONB)
    last_reason:Mapped[str|None]=mapped_column(String(120))
    updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    __table_args__=(UniqueConstraint('manifest_id','source','adjustment','output_start','output_end',name='uq_supplier_price_refresh_window'),)
