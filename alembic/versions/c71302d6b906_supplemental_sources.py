"""Add independent immutable supplemental source staging."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision='c71302d6b906'
down_revision='c71302d6b905'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('supplemental_document',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('source_contract_id',sa.String(36),sa.ForeignKey('source_contract.id'),nullable=False),
        sa.Column('raw_id',sa.String(36),sa.ForeignKey('raw_object.id'),nullable=False),
        sa.Column('source',sa.String(80),nullable=False),sa.Column('domain',sa.String(80),nullable=False),
        sa.Column('source_url',sa.Text(),nullable=False),sa.Column('content_hash',sa.String(64),nullable=False),
        sa.Column('signature',sa.String(64),nullable=False,unique=True),
        sa.Column('captured_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('request_scope',postgresql.JSONB(),nullable=False),sa.Column('evidence',postgresql.JSONB(),nullable=False))
    op.create_index('ix_supplemental_document_source_domain','supplemental_document',['source','domain'])
    op.create_table('supplemental_observation',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('document_id',sa.String(36),sa.ForeignKey('supplemental_document.id'),nullable=False),
        sa.Column('record_kind',sa.String(40),nullable=False),sa.Column('entity_key',sa.Text(),nullable=False),sa.Column('row_key',sa.Text(),nullable=False),
        *(sa.Column(n,sa.Text(),nullable=True) for n in ('entity_name','member_identifier','member_name')),
        sa.Column('product_type',sa.String(80),nullable=True),sa.Column('currency',sa.String(30),nullable=True),
        *(sa.Column(n,sa.Date(),nullable=True) for n in ('report_date','filing_date','published_date','announced_date','effective_date','ex_date','record_date','pay_date','maturity_date')),
        *(sa.Column(n,sa.DateTime(timezone=True),nullable=True) for n in ('published_at','event_timestamp')),
        *(sa.Column(n,sa.Numeric(),nullable=True) for n in ('quantity','value','weight','nav','net_assets','total_assets','total_liabilities','fee_rate','price','sum_price_volume','eligible_volume')),
        sa.Column('captured_at',sa.DateTime(timezone=True),nullable=False),sa.Column('date_basis',sa.String(120),nullable=False),
        sa.Column('fields',postgresql.JSONB(),nullable=False),sa.Column('issues',postgresql.JSONB(),nullable=False),
        sa.Column('canonical_eligible',sa.Boolean(),nullable=False),sa.Column('signature',sa.String(64),nullable=False,unique=True),
        sa.CheckConstraint('canonical_eligible = false',name=op.f('ck_supplemental_observation_source_stage_only')))
    op.create_index('ix_supplemental_observation_doc_kind','supplemental_observation',['document_id','record_kind'])
    for table in ('supplemental_document','supplemental_observation'):
        op.execute(f'CREATE TRIGGER immutable_evidence BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION backtest_immutable()')

def downgrade():
    op.drop_table('supplemental_observation')
    op.drop_table('supplemental_document')
