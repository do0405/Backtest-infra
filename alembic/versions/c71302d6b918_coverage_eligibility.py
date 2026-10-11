"""Add immutable calendar/listing assertions and bounded eligibility pages."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c71302d6b918"
down_revision = "c71302d6b917"
branch_labels = None
depends_on = None


def _evidence_clocks():
    return [sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_version", sa.String(80), nullable=False),
        sa.Column("assertion_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("source_refs", postgresql.JSONB(), nullable=False)]


def upgrade():
    op.create_table("calendar_eligibility_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("calendar_version", sa.String(80), nullable=False),
        sa.Column("start", sa.Date(), nullable=False), sa.Column("end", sa.Date(), nullable=False),
        sa.Column("rows", postgresql.JSONB(), nullable=False),
        sa.Column("complete_interval", sa.Boolean(), nullable=False),
        sa.Column("authoritative", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False), *_evidence_clocks(),
        sa.CheckConstraint('"start" <= "end"', name="ordered_interval"),
        sa.CheckConstraint("recorded_at >= captured_at", name="capture_record_order"),
        sa.CheckConstraint("status IN ('accepted','rejected')", name="status"),
        sa.CheckConstraint("NOT complete_interval OR jsonb_array_length(rows) > 0", name="nonempty_complete"))
    op.create_index("ix_calendar_eligibility_market_cutoff", "calendar_eligibility_evidence", ["market", "recorded_at", "captured_at"])
    op.create_table("listing_eligibility_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("security_id", sa.String(36), sa.ForeignKey("security.id"), nullable=False),
        sa.Column("market", sa.String(10), nullable=False), sa.Column("source", sa.String(80), nullable=False),
        sa.Column("listing_start", sa.Date(), nullable=False), sa.Column("listing_end", sa.Date(), nullable=True),
        sa.Column("scope_start", sa.Date(), nullable=False), sa.Column("scope_end", sa.Date(), nullable=False),
        sa.Column("complete_history", sa.Boolean(), nullable=False),
        sa.Column("initial_listing_declared", sa.Boolean(), nullable=False),
        sa.Column("lifecycle_ref", sa.String(200), nullable=True),
        sa.Column("identity_status", sa.String(40), nullable=False),
        sa.Column("grade", sa.String(40), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        *_evidence_clocks(), sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("scope_start <= scope_end", name="ordered_scope"),
        sa.CheckConstraint("listing_end IS NULL OR listing_start < listing_end", name="ordered_listing"),
        sa.CheckConstraint("recorded_at >= captured_at", name="capture_record_order"),
        sa.CheckConstraint("status IN ('accepted','rejected')", name="status"),
        sa.CheckConstraint("grade IN ('verified','source_declared')", name="grade"),
        sa.CheckConstraint("identity_status IN ('verified_security','source_declared_security','unresolved_symbol')", name="identity"))
    op.create_index("ix_listing_eligibility_security_cutoff", "listing_eligibility_evidence", ["security_id", "recorded_at", "captured_at"])
    op.create_table("coverage_eligibility_overlay",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("coverage_run.id"), nullable=False),
        sa.Column("baseline_manifest_hash", sa.String(64), nullable=False),
        sa.Column("baseline_watermark", sa.DateTime(timezone=True), nullable=False),
        sa.Column("baseline_target_count", sa.Integer(), nullable=False),
        sa.Column("output_start", sa.Date(), nullable=False), sa.Column("output_end", sa.Date(), nullable=False),
        sa.Column("evidence_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("profile", sa.String(80), nullable=False), sa.Column("policy_version", sa.String(80), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_eligibility_overlay_run_cutoff", "coverage_eligibility_overlay", ["run_id", "evidence_cutoff"])
    op.create_table("coverage_eligibility_page",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("overlay_id", sa.String(36), sa.ForeignKey("coverage_eligibility_overlay.id"), nullable=False),
        sa.Column("start_cursor", sa.Integer(), nullable=False), sa.Column("end_cursor", sa.Integer(), nullable=False),
        sa.Column("member_limit", sa.Integer(), nullable=False),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False), sa.Column("members", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("overlay_id", "start_cursor"),
        sa.CheckConstraint("start_cursor >= 0 AND end_cursor > start_cursor", name="ordered_cursor"),
        sa.CheckConstraint("member_limit BETWEEN 1 AND 100 AND jsonb_array_length(members) <= member_limit", name="bounded_page"))
    op.create_index("ix_eligibility_page_overlay_end", "coverage_eligibility_page", ["overlay_id", "end_cursor"])
    for table in ("calendar_eligibility_evidence", "listing_eligibility_evidence", "coverage_eligibility_overlay", "coverage_eligibility_page"):
        op.execute(f"CREATE TRIGGER immutable_evidence BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION backtest_immutable()")


def downgrade():
    for table in ("coverage_eligibility_page", "coverage_eligibility_overlay", "listing_eligibility_evidence", "calendar_eligibility_evidence"):
        op.drop_table(table)
