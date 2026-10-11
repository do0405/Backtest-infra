"""Independent remote stream receipts and contiguous local import cursors."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c71302d6b910"
down_revision = "c71302d6b909"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("remote_import_receipt",
        sa.Column("bundle_id", sa.String(36), primary_key=True),
        sa.Column("stream_id", sa.String(80), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("inserted_rows", sa.Integer(), nullable=False),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("checkpoint", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("stream_id", "sequence"))
    op.execute("CREATE TRIGGER immutable_evidence BEFORE UPDATE OR DELETE ON remote_import_receipt FOR EACH ROW EXECUTE FUNCTION backtest_immutable()")
    op.create_table("remote_stream_cursor",
        sa.Column("stream_id", sa.String(80), primary_key=True),
        sa.Column("committed_sequence", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("remote_stream_cursor")
    op.drop_table("remote_import_receipt")
