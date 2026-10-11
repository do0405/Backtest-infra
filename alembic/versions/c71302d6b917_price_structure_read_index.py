"""Index structural price eligibility using bounded identity/date keys only."""
from alembic import op

revision = "c71302d6b917"
down_revision = "c71302d6b916"
branch_labels = depends_on = None

INDEX_NAME = "ix_price_observation_clean_structure_read"
PREDICATE_SQL = """basis = 'as_traded' AND quantity_unit = 'shares'
    AND open > 0 AND close > 0 AND low > 0 AND high >= low
    AND high >= open AND high >= close AND low <= open AND low <= close
    AND volume IS NOT NULL AND volume >= 0
    AND CAST(open AS VARCHAR) NOT IN ('NaN', 'Infinity', '-Infinity')
    AND CAST(high AS VARCHAR) NOT IN ('NaN', 'Infinity', '-Infinity')
    AND CAST(low AS VARCHAR) NOT IN ('NaN', 'Infinity', '-Infinity')
    AND CAST(close AS VARCHAR) NOT IN ('NaN', 'Infinity', '-Infinity')
    AND CAST(volume AS VARCHAR) NOT IN ('NaN', 'Infinity', '-Infinity')"""

# PostgreSQL's stored expression includes type coercions and ALL-array syntax.
# Compare its full frozen expression, rather than accepting any partial index
# with the same key fields. Whitespace alone is irrelevant.
EXPECTED_PREDICATE = "(" + " AND ".join((
    "((basis)::text = 'as_traded'::text)",
    "((quantity_unit)::text = 'shares'::text)",
    "(open > (0)::numeric)", "(close > (0)::numeric)", "(low > (0)::numeric)",
    "(high >= low)", "(high >= open)", "(high >= close)", "(low <= open)", "(low <= close)",
    "(volume IS NOT NULL)", "(volume >= (0)::numeric)",
    *(f"((({column})::character varying)::text <> ALL ((ARRAY['NaN'::character varying, 'Infinity'::character varying, '-Infinity'::character varying])::text[]))"
      for column in ("open", "high", "low", "close", "volume")),
)) + ")"


def _guard(*, required):
    missing = f"IF idx IS NULL THEN RAISE EXCEPTION '{INDEX_NAME} missing; preserve and inspect'; END IF;" if required else ""
    expected = EXPECTED_PREDICATE.replace("'", "''")
    return f"""DO $$ DECLARE idx regclass := to_regclass('{INDEX_NAME}'); BEGIN
        {missing}
        IF idx IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM pg_index i JOIN pg_class ic ON ic.oid=i.indexrelid
            JOIN pg_am am ON am.oid=ic.relam
            WHERE i.indexrelid=idx AND i.indrelid='price_observation'::regclass
                AND am.amname='btree' AND i.indisvalid AND i.indisready AND i.indislive
                AND NOT i.indisunique AND NOT i.indisexclusion
                AND i.indnkeyatts=4 AND i.indnatts=4 AND i.indexprs IS NULL
                AND i.indoption[0]=0 AND i.indoption[1]=0 AND i.indoption[2]=0 AND i.indoption[3]=0
                AND (SELECT array_agg(a.attname::text ORDER BY k.ordinality)
                    FROM unnest(i.indkey) WITH ORDINALITY k(attnum,ordinality)
                    JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum)
                    = ARRAY['security_id','session','captured_at','id']::text[]
                AND regexp_replace(pg_get_expr(i.indpred,i.indrelid), '\\s+', '', 'g')
                    = regexp_replace('{expected}', '\\s+', '', 'g')
                AND NOT EXISTS (
                    SELECT 1 FROM unnest(i.indkey) WITH ORDINALITY k(attnum,ordinality)
                    JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
                    JOIN pg_opclass opc ON opc.oid=i.indclass[k.ordinality-1]
                    WHERE NOT opc.opcdefault OR i.indcollation[k.ordinality-1]<>a.attcollation))
        THEN RAISE EXCEPTION '{INDEX_NAME} invalid or definition differs; preserve and inspect';
        END IF;
        END $$"""


def upgrade():
    op.execute(_guard(required=False))
    with op.get_context().autocommit_block():
        op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
            "ON price_observation (security_id, session, captured_at, id) WHERE " + PREDICATE_SQL)
    op.execute(_guard(required=True))


def downgrade():
    op.execute(_guard(required=False))
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS " + INDEX_NAME)
