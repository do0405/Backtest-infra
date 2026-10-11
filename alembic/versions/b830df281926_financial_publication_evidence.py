"""Append immutable publication certificates without changing legacy evidence."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "b830df281926"
down_revision = "6c347adb8181"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("financial_publication_evidence",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("document_id", sa.String(36), nullable=False),
        sa.Column("filing_id", sa.String(36), nullable=False),
        sa.Column("issuer_id", sa.String(36), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("accession", sa.String(100), nullable=False),
        sa.Column("document_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("public_day", sa.Date(), nullable=True),
        sa.Column("effective_available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("precision", sa.String(40), nullable=False),
        sa.Column("timezone", sa.String(80), nullable=True),
        sa.Column("policy_version", sa.String(80), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assertion_hash", sa.String(64), nullable=False),
        sa.Column("supersedes_assertion_hash", sa.String(64), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("status IN ('qualified', 'unavailable', 'rejected', 'withdrawal')",
            name=op.f("ck_financial_publication_evidence_status")),
        sa.CheckConstraint("recorded_at >= captured_at", name=op.f("ck_financial_publication_evidence_capture_record_order")),
        sa.CheckConstraint("status NOT IN ('qualified', 'withdrawal') OR "
            "(public_day IS NOT NULL AND effective_available_at IS NOT NULL AND precision = 'public_day' "
            "AND timezone IS NOT NULL)", name=op.f("ck_financial_publication_evidence_public_bound")),
        sa.CheckConstraint("status NOT IN ('rejected', 'withdrawal') OR supersedes_assertion_hash IS NOT NULL",
            name=op.f("ck_financial_publication_evidence_event_target")),
        sa.ForeignKeyConstraint(["document_id"], ["financial_source_document.id"],
            name=op.f("fk_financial_publication_evidence_document_id_financial_source_document")),
        sa.ForeignKeyConstraint(["filing_id"], ["filing.id"], name=op.f("fk_financial_publication_evidence_filing_id_filing")),
        sa.ForeignKeyConstraint(["issuer_id"], ["issuer.id"], name=op.f("fk_financial_publication_evidence_issuer_id_issuer")),
        sa.ForeignKeyConstraint(["supersedes_assertion_hash"], ["financial_publication_evidence.assertion_hash"],
            name=op.f("fk_financial_publication_evidence_supersedes_assertion_hash_financial_publication_evidence")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_financial_publication_evidence")),
        sa.UniqueConstraint("assertion_hash", name=op.f("uq_financial_publication_evidence_assertion_hash")))
    for name in ("document_id", "filing_id", "issuer_id"):
        op.create_index(op.f(f"ix_financial_publication_evidence_{name}"), "financial_publication_evidence", [name])
    op.execute("CREATE TRIGGER immutable_evidence BEFORE UPDATE OR DELETE ON financial_publication_evidence "
        "FOR EACH ROW EXECUTE FUNCTION backtest_immutable()")


def downgrade():
    op.execute("DROP TRIGGER immutable_evidence ON financial_publication_evidence")
    for name in ("issuer_id", "filing_id", "document_id"):
        op.drop_index(op.f(f"ix_financial_publication_evidence_{name}"), table_name="financial_publication_evidence")
    op.drop_table("financial_publication_evidence")
