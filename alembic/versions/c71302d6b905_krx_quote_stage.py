"""Add immutable typed KRX source rows without assigning historical identities."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c71302d6b905"
down_revision = "c71302d6b904"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("krx_quote_observation",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("source_contract_id", sa.String(36), nullable=False),
        sa.Column("raw_id", sa.String(36), nullable=False),
        sa.Column("response_capture_id", sa.String(36), nullable=False),
        sa.Column("service", sa.String(40), nullable=False),
        sa.Column("record_kind", sa.String(40), nullable=False),
        sa.Column("request_date", sa.Date(), nullable=False),
        sa.Column("reported_date", sa.Date(), nullable=True),
        sa.Column("source_code", sa.Text(), nullable=True),
        sa.Column("source_name", sa.Text(), nullable=True),
        sa.Column("source_isin", sa.Text(), nullable=True),
        sa.Column("source_english_name", sa.Text(), nullable=True),
        sa.Column("index_class", sa.Text(), nullable=True),
        sa.Column("listing_date", sa.Date(), nullable=True),
        sa.Column("row_index", sa.Integer(), nullable=False),
        *(sa.Column(name, sa.Numeric(), nullable=True) for name in (
            "open", "high", "low", "close", "volume", "notional", "market_cap", "listed_shares",
            "price_change", "change_rate", "nav", "net_asset_total", "indicative_value", "indicative_value_total",
            "underlying_index", "index_change", "index_change_rate", "par_value")),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("raw_fields", postgresql.JSONB(), nullable=False),
        sa.Column("issues", postgresql.JSONB(), nullable=False),
        sa.Column("signature", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("coverage_complete", sa.Boolean(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("row_index >= 0", name=op.f("ck_krx_quote_observation_nonnegative_row_index")),
        sa.CheckConstraint("coverage_complete = false", name=op.f("ck_krx_quote_observation_coverage_unqualified")),
        sa.CheckConstraint("record_kind IN ('daily_quote', 'basic_information', 'index_quote')", name=op.f("ck_krx_quote_observation_record_kind")),
        sa.CheckConstraint("status IN ('source_typed', 'source_issues')", name=op.f("ck_krx_quote_observation_source_status")),
        sa.ForeignKeyConstraint(["source_contract_id"], ["source_contract.id"], name="fk_krx_quote_observation_source_contract_id_source_contract"),
        sa.ForeignKeyConstraint(["raw_id"], ["raw_object.id"], name="fk_krx_quote_observation_raw_id_raw_object"),
        sa.ForeignKeyConstraint(["response_capture_id"], ["ingestion_attempt.id"], name="fk_krx_quote_observation_response_capture_id_ingestion_attempt"),
        sa.PrimaryKeyConstraint("id", name="pk_krx_quote_observation"),
        sa.UniqueConstraint("signature", name="uq_krx_quote_observation_signature"))
    op.create_index("ix_krx_quote_service_date_capture", "krx_quote_observation", ["service", "request_date", "captured_at"])
    op.create_index("ix_krx_quote_response_capture", "krx_quote_observation", ["response_capture_id"])
    op.execute("CREATE TRIGGER immutable_evidence BEFORE UPDATE OR DELETE ON krx_quote_observation "
               "FOR EACH ROW EXECUTE FUNCTION backtest_immutable()")


def downgrade():
    op.drop_table("krx_quote_observation")
