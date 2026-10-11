"""Bounded trade pagination and exact deduplication."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
revision = 'c71302d6b914'
down_revision = 'c71302d6b913'
branch_labels = depends_on = None


def upgrade():
    op.create_table('vwap_trade_run',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('security_id',sa.String(36),sa.ForeignKey('security.id'),nullable=False),
        sa.Column('symbol',sa.String(120),nullable=False),sa.Column('session',sa.Date,nullable=False),
        sa.Column('rule',sa.String(80),nullable=False),sa.Column('request_hash',sa.String(64),unique=True,nullable=False),
        sa.Column('status',sa.String(40),nullable=False),sa.Column('cursor',JSONB,nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False))
    op.create_table('vwap_trade_event',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('run_id',sa.String(36),sa.ForeignKey('vwap_trade_run.id'),nullable=False),
        sa.Column('document_id',sa.String(36),sa.ForeignKey('supplemental_document.id'),nullable=False),
        sa.Column('event_key',sa.String(64),nullable=False),sa.Column('fingerprint',sa.String(64),nullable=False),
        sa.Column('timestamp_second',sa.DateTime(timezone=True),nullable=False),
        sa.Column('timestamp_nanosecond',sa.Integer,nullable=False),
        sa.Column('price',sa.Numeric,nullable=False),sa.Column('quantity',sa.Numeric,nullable=False),
        sa.Column('numerator',sa.Numeric,nullable=False),sa.Column('eligible',sa.Boolean,nullable=False),
        sa.UniqueConstraint('run_id','event_key',name='uq_vwap_trade_run_event'))
    op.create_index('ix_vwap_trade_event_run_id','vwap_trade_event',['run_id'])


def downgrade():
    op.drop_table('vwap_trade_event')
    op.drop_table('vwap_trade_run')
