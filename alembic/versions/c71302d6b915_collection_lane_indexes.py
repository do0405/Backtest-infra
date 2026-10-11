"""Seek runnable lanes and exact source requests without scanning the queue."""
from alembic import op

revision = "c71302d6b915"
down_revision = "c71302d6b914"
branch_labels = depends_on = None

INDEXES = ("ix_collection_job_runnable_lane", "ix_collection_job_source_entity")


def upgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_index
            WHERE indexrelid IN (to_regclass('ix_collection_job_runnable_lane'),
                                to_regclass('ix_collection_job_source_entity')) AND NOT indisvalid)
        THEN RAISE EXCEPTION 'collection lookup index is invalid; preserve and inspect before explicit repair';
        END IF;
        END $$""")
    with op.get_context().autocommit_block():
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_collection_job_runnable_lane "
                   "ON collection_job (source,domain,attempts,priority,id) "
                   "WHERE status IN ('pending','retry','running')")
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_collection_job_source_entity "
                   "ON collection_job (source,domain,entity_id)")
    op.execute("""DO $$ BEGIN
        IF (SELECT count(*) FROM pg_index
            WHERE indexrelid IN (to_regclass('ix_collection_job_runnable_lane'),
                                to_regclass('ix_collection_job_source_entity')) AND indisvalid) != 2
        THEN RAISE EXCEPTION 'collection lookup index build incomplete; preserve and inspect';
        END IF;
        END $$""")


def downgrade():
    with op.get_context().autocommit_block():
        for name in reversed(INDEXES):
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS " + name)
