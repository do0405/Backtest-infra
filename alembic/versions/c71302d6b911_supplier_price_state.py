"""Independent provider adjusted bar page checkpoint."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
revision='c71302d6b911'
down_revision='c71302d6b910'
branch_labels=None
depends_on=None
def upgrade():
    op.create_table('supplier_price_collection_state',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('manifest_id',sa.String(36),sa.ForeignKey('target_universe_manifest.id'),nullable=False),
        sa.Column('source',sa.String(80),nullable=False),sa.Column('adjustment',sa.String(80),nullable=False),
        sa.Column('target_hash',sa.String(64),nullable=False),sa.Column('target_count',sa.Integer,nullable=False),
        sa.Column('market_target_count',sa.Integer,nullable=False),sa.Column('output_start',sa.Date,nullable=False),
        sa.Column('output_end',sa.Date,nullable=False),sa.Column('status',sa.String(40),nullable=False),
        sa.Column('cursor',JSONB,nullable=False),sa.Column('last_reason',sa.String(120)),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('manifest_id','source','adjustment',name='uq_supplier_price_target_basis'))
def downgrade():op.drop_table('supplier_price_collection_state')
