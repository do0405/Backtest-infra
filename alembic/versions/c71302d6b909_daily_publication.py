"""Add independent daily metadata-grade financial publication assertions."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision = "c71302d6b909"
down_revision = "c71302d6b908"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("financial_daily_publication_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("financial_source_document.id"), nullable=False),
        sa.Column("source_fact_id", sa.String(36), sa.ForeignKey("source_financial_fact.id"), nullable=True),
        sa.Column("filing_id", sa.String(36), sa.ForeignKey("filing.id"), nullable=False),
        sa.Column("issuer_id", sa.String(36), sa.ForeignKey("issuer.id"), nullable=False),
        sa.Column("source", sa.String(80), nullable=False), sa.Column("market", sa.String(10), nullable=False),
        sa.Column("source_version_ref", sa.String(100), nullable=False),
        sa.Column("status", sa.String(40), nullable=False), sa.Column("reason", sa.String(200), nullable=True),
        sa.Column("public_day", sa.Date(), nullable=True),
        sa.Column("effective_available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_grade", sa.String(40), nullable=False),
        sa.Column("metadata_date_basis", sa.String(80), nullable=False),
        sa.Column("calendar_basis", sa.String(120), nullable=False),
        sa.Column("policy_version", sa.String(80), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assertion_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("status IN ('qualified','unavailable')", name=op.f("ck_financial_daily_publication_evidence_daily_status")),
        sa.CheckConstraint("evidence_grade = 'metadata_grade'", name=op.f("ck_financial_daily_publication_evidence_metadata_grade")),
        sa.CheckConstraint("recorded_at >= captured_at", name=op.f("ck_financial_daily_publication_evidence_daily_capture_record_order")),
        sa.CheckConstraint("status != 'qualified' OR (public_day IS NOT NULL AND effective_available_at IS NOT NULL)", name=op.f("ck_financial_daily_publication_evidence_daily_public_bound")))
    op.create_index("ix_daily_publication_document_policy", "financial_daily_publication_evidence", ["document_id", "policy_version"])
    op.create_index("ix_daily_publication_source_fact", "financial_daily_publication_evidence", ["source_fact_id"])
    op.execute("CREATE TRIGGER immutable_evidence BEFORE UPDATE OR DELETE ON financial_daily_publication_evidence FOR EACH ROW EXECUTE FUNCTION backtest_immutable()")


def downgrade():
    op.drop_table("financial_daily_publication_evidence")
