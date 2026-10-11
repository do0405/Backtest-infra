"""Persist rolling free provider call budget without credential values."""
from alembic import op
import sqlalchemy as sa
revision='c71302d6b907'
down_revision='c71302d6b906'
branch_labels=None
depends_on=None
def upgrade():
    op.create_table('supplemental_api_call',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('source', sa.String(80), nullable=False),
        sa.Column('attempted_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_supplemental_api_call_source_time','supplemental_api_call',['source','attempted_at'])
    op.execute('CREATE TRIGGER immutable_evidence BEFORE UPDATE OR DELETE ON supplemental_api_call FOR EACH ROW EXECUTE FUNCTION backtest_immutable()')
def downgrade():
    op.drop_table('supplemental_api_call')
