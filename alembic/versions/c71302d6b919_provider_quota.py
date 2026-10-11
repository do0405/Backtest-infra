"""Add provider admission controls without changing legacy rate columns."""
from alembic import op

from backtest_data.ingestion.quota_sql import install_quota_schema

revision = 'c71302d6b919'
down_revision = 'c71302d6b918'
branch_labels = None
depends_on = None


def upgrade():
    install_quota_schema(op.get_bind())


def downgrade():
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute('DROP TRIGGER IF EXISTS provider_quota_admission ON rate_budget')
    op.execute('DROP FUNCTION IF EXISTS backtest_provider_quota_admit()')
    op.drop_table('provider_quota_allocation_day')
    op.drop_table('provider_quota_reservation')
    op.drop_table('provider_quota_policy')
