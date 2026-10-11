"""Add resumable whole-universe coverage snapshots, summaries, and gaps."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c71302d6b902"
down_revision = "b830df281926"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("coverage_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("manifest_id", sa.String(36), sa.ForeignKey("target_universe_manifest.id"), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(80), nullable=False),
        sa.Column("output_start", sa.Date(), nullable=False),
        sa.Column("output_end", sa.Date(), nullable=False),
        sa.Column("watermark", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("target_count", sa.Integer(), nullable=False),
        sa.Column("processed_count", sa.Integer(), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False))
    op.create_table("coverage_member",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("coverage_run.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("security_id", sa.String(36), nullable=True),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("member", postgresql.JSONB(), nullable=False),
        sa.Column("done", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_coverage_member_run_id"))
    op.create_table("coverage_summary",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("coverage_run.id"), nullable=False),
        sa.Column("member_id", sa.String(36), sa.ForeignKey("coverage_member.id"), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("domain", sa.String(40), nullable=False),
        sa.Column("observed_count", sa.Integer(), nullable=False),
        sa.Column("clean_count", sa.Integer(), nullable=False),
        sa.Column("expected_count", sa.Integer(), nullable=True),
        sa.Column("missing_count", sa.Integer(), nullable=True),
        sa.Column("coverage_complete", sa.Boolean(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("member_id", "domain", name="uq_coverage_summary_member_id"))
    op.create_table("coverage_gap",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("coverage_run.id"), nullable=False),
        sa.Column("member_id", sa.String(36), sa.ForeignKey("coverage_member.id"), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("domain", sa.String(40), nullable=False),
        sa.Column("reason", sa.String(120), nullable=False),
        sa.Column("count", sa.Integer(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=False))
    op.create_table("coverage_financial_period",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("coverage_run.id"), nullable=False),
        sa.Column("member_id", sa.String(36), sa.ForeignKey("coverage_member.id"), nullable=False),
        sa.Column("period_key", sa.String(64), nullable=False),
        sa.Column("period_kind", sa.String(40), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("instant", sa.Date(), nullable=True),
        sa.Column("source_fact_count", sa.Integer(), nullable=False),
        sa.Column("qualified_source_fact_count", sa.Integer(), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("qualified_document_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(80), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("member_id", "period_key", name="uq_coverage_financial_period_member_id"))
    for table, columns in {"coverage_member": ("run_id", "security_id"),
            "coverage_summary": ("run_id", "member_id"),
            "coverage_gap": ("run_id", "member_id", "reason"),
            "coverage_financial_period": ("run_id", "member_id")}.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    for table in ("coverage_financial_period", "coverage_gap", "coverage_summary", "coverage_member", "coverage_run"):
        op.drop_table(table)
