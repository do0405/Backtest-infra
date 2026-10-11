"""Index bounded financial-completion annotation lookups concurrently."""
from alembic import op

revision = "c71302d6b904"
down_revision = "c71302d6b903"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_validation_issue_financial_completion_lookup"


def upgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_index
            WHERE indexrelid = to_regclass('ix_validation_issue_financial_completion_lookup') AND NOT indisvalid)
        THEN RAISE EXCEPTION 'financial annotation index is invalid; inspect read-only and request explicit repair';
        END IF;
        END $$""")
    with op.get_context().autocommit_block():
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS " + INDEX_NAME + " ON validation_issue "
            "(record_domain, record_id, rule, created_at DESC, id DESC)")
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_index
            WHERE indexrelid = to_regclass('ix_validation_issue_financial_completion_lookup') AND indisvalid)
        THEN RAISE EXCEPTION 'financial annotation index build is incomplete; inspect read-only before retry';
        END IF;
        END $$""")


def downgrade():
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS " + INDEX_NAME)
