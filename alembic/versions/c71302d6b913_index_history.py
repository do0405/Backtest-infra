"""Independent immutable official index source history and bounded cursors."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = 'c71302d6b913'
down_revision = 'c71302d6b912'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('index_history_record',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('document_id',sa.String(36),sa.ForeignKey('supplemental_document.id'),nullable=False),
        sa.Column('record_kind',sa.String(40),nullable=False),sa.Column('index_key',sa.Text(),nullable=False),
        sa.Column('row_key',sa.Text(),nullable=False),sa.Column('member_identifier',sa.Text()),sa.Column('member_name',sa.Text()),
        sa.Column('action',sa.String(20)),
        *(sa.Column(n,sa.Date()) for n in ('announced_date','published_date','effective_date','snapshot_date','weight_date')),
        sa.Column('effective_phase',sa.String(40)),sa.Column('weight',sa.Numeric()),sa.Column('weight_unit',sa.String(30)),
        sa.Column('captured_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('fields',JSONB,nullable=False),sa.Column('issues',JSONB,nullable=False),
        sa.Column('canonical_eligible',sa.Boolean(),nullable=False),sa.Column('signature',sa.String(64),nullable=False,unique=True),
        sa.CheckConstraint('canonical_eligible = false',name=op.f('ck_index_history_record_source_stage_only')),
        sa.CheckConstraint("record_kind IN ('snapshot_member','change','announcement','access_gap')",name=op.f('ck_index_history_record_known_record_kind')),
        sa.CheckConstraint("action IS NULL OR action IN ('add','delete')",name=op.f('ck_index_history_record_known_action')),
        sa.CheckConstraint("weight IS NULL OR (weight >= 0 AND weight_date IS NOT NULL AND weight_unit IN ('fraction','percent'))",name=op.f('ck_index_history_record_dated_weight_unit')))
    op.create_index('ix_index_history_index_dates','index_history_record',['index_key','published_date','effective_date'])
    op.create_index('ix_index_history_document_kind','index_history_record',['document_id','record_kind'])
    op.execute('CREATE TRIGGER immutable_evidence BEFORE UPDATE OR DELETE ON index_history_record FOR EACH ROW EXECUTE FUNCTION backtest_immutable()')
    op.create_table('index_acquisition_state',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('manifest_id',sa.String(36),sa.ForeignKey('target_universe_manifest.id'),nullable=False),
        sa.Column('job_key',sa.String(80),nullable=False),sa.Column('target_hash',sa.String(64),nullable=False),
        sa.Column('target_count',sa.Integer(),nullable=False),sa.Column('output_start',sa.Date(),nullable=False),
        sa.Column('output_end',sa.Date(),nullable=False),sa.Column('cursor',JSONB,nullable=False),
        sa.Column('status',sa.String(40),nullable=False),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('manifest_id','job_key',name='uq_index_acquisition_manifest_job'))

def downgrade():
    op.drop_table('index_acquisition_state')
    op.drop_table('index_history_record')
