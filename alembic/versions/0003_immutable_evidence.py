"""Enforce immutable evidence and sealed membership in PostgreSQL."""
from alembic import op

revision = "0003_immutable_evidence"
down_revision = "f2342869944a"
branch_labels = None
depends_on = None

IMMUTABLE = (
    "raw_object", "ingestion_attempt", "source_contract", "identifier_history", "exchange_session",
    "target_universe_manifest", "filing", "financial_source_document", "source_financial_fact",
    "fundamental_fact", "derived_fact", "fund_product_fact", "price_observation",
    "corporate_action_observation", "vwap_daily_stat", "selection_decision", "data_segment",
    "segment_member", "release_segment", "validation_issue",
)


def upgrade():
    op.execute("""
    CREATE FUNCTION backtest_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'immutable evidence cannot be modified'; END; $$;
    """)
    for table in IMMUTABLE:
        op.execute(f"CREATE TRIGGER immutable_evidence BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION backtest_immutable()")
    op.execute("""
    CREATE FUNCTION backtest_release_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'UPDATE' AND OLD.status = 'building' AND NEW.status = 'sealed'
        AND (to_jsonb(NEW) - 'status') = (to_jsonb(OLD) - 'status') THEN RETURN NEW; END IF;
      RAISE EXCEPTION 'immutable sealed release';
    END; $$;
    CREATE TRIGGER immutable_release BEFORE UPDATE OR DELETE ON dataset_release
      FOR EACH ROW EXECUTE FUNCTION backtest_release_guard();
    CREATE FUNCTION backtest_member_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE release_status text;
    BEGIN
      PERFORM 1 FROM data_segment WHERE id = NEW.segment_id FOR UPDATE;
      IF TG_TABLE_NAME = 'segment_member' THEN
        IF EXISTS (SELECT 1 FROM release_segment WHERE segment_id = NEW.segment_id) THEN
          RAISE EXCEPTION 'immutable referenced segment membership'; END IF;
      ELSE
        SELECT status INTO release_status FROM dataset_release WHERE id = NEW.release_id FOR UPDATE;
        IF release_status IS DISTINCT FROM 'building' THEN RAISE EXCEPTION 'immutable sealed release membership'; END IF;
      END IF;
      RETURN NEW;
    END; $$;
    CREATE TRIGGER segment_insert_guard BEFORE INSERT ON segment_member
      FOR EACH ROW EXECUTE FUNCTION backtest_member_guard();
    CREATE TRIGGER release_insert_guard BEFORE INSERT ON release_segment
      FOR EACH ROW EXECUTE FUNCTION backtest_member_guard();
    """)


def downgrade():
    op.execute("DROP TRIGGER release_insert_guard ON release_segment")
    op.execute("DROP TRIGGER segment_insert_guard ON segment_member")
    op.execute("DROP FUNCTION backtest_member_guard()")
    op.execute("DROP TRIGGER immutable_release ON dataset_release")
    op.execute("DROP FUNCTION backtest_release_guard()")
    for table in IMMUTABLE:
        op.execute(f"DROP TRIGGER immutable_evidence ON {table}")
    op.execute("DROP FUNCTION backtest_immutable()")
