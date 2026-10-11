"""Cover latest price decisions without per-row decision heap/JSON reads.

The concurrent build can take substantial time on a populated database. A
cancelled build may leave an invalid index; preserve it for read-only inspection
and explicit operator repair rather than automatically dropping/rebuilding it.
"""
from alembic import op

revision = "c71302d6b903"
down_revision = "c71302d6b902"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_selection_decision_price_coverage_latest"


def upgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_index
            WHERE indexrelid IN (to_regclass('ix_selection_decision_price_coverage_latest'),
                to_regclass('ix_collection_job_coverage_entity')) AND NOT indisvalid)
        THEN RAISE EXCEPTION 'coverage query index is invalid; inspect read-only and request explicit repair';
        END IF;
        END $$""")
    with op.get_context().autocommit_block():
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS " + INDEX_NAME + " ON selection_decision "
            "(record_id, recorded_at DESC, id DESC) INCLUDE (status, rule_version, evidence_grade) "
            "WHERE record_domain = 'price_observation'")
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_collection_job_coverage_entity ON collection_job (entity_id)")
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_index
            WHERE indexrelid = to_regclass('ix_selection_decision_price_coverage_latest') AND indisvalid)
        OR NOT EXISTS (SELECT 1 FROM pg_index
            WHERE indexrelid = to_regclass('ix_collection_job_coverage_entity') AND indisvalid)
        THEN RAISE EXCEPTION 'coverage query index build is incomplete; inspect read-only before retry';
        END IF;
        END $$""")


def downgrade():
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_collection_job_coverage_entity")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS " + INDEX_NAME)
