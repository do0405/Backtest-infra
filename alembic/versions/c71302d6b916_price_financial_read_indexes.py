"""Seek price windows and cover financial periods without fetching source bodies."""
from alembic import op

revision = "c71302d6b916"
down_revision = "c71302d6b915"
branch_labels = depends_on = None

INDEXES = (
    ("ix_price_observation_coverage_read", "price_observation",
     ("security_id", "session", "source_contract_id", "captured_at", "id"),
     ("basis", "quantity_unit"), (0, 0, 0, 3, 3)),
    ("ix_source_financial_fact_period_read", "source_financial_fact",
     ("document_id", "id"),
     ("issuer_id", "period_kind", "period_start", "period_end", "instant", "captured_at", "extracted_at", "first_seen_at"),
     (0, 0)),
)


def _guard(spec, *, required):
    name, table, keys, included, options = spec
    columns = ",".join("'" + column + "'" for column in (*keys, *included))
    order = " AND ".join(f"i.indoption[{ordinal}]={option}" for ordinal, option in enumerate(options))
    missing = f"IF idx IS NULL THEN RAISE EXCEPTION '{name} missing; preserve and inspect'; END IF;" if required else ""
    return f"""DO $$ DECLARE idx regclass := to_regclass('{name}'); BEGIN
        {missing}
        IF idx IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM pg_index i JOIN pg_class ic ON ic.oid=i.indexrelid
            JOIN pg_am am ON am.oid=ic.relam
            WHERE i.indexrelid=idx AND i.indrelid='{table}'::regclass
                AND am.amname='btree' AND i.indisvalid AND i.indisready AND i.indislive
                AND NOT i.indisunique AND NOT i.indisexclusion
                AND i.indnkeyatts={len(keys)} AND i.indnatts={len(keys)+len(included)}
                AND NOT (i.indpred IS NOT NULL OR i.indexprs IS NOT NULL)
                AND {order}
                AND (SELECT array_agg(a.attname::text ORDER BY k.ordinality)
                    FROM unnest(i.indkey) WITH ORDINALITY k(attnum,ordinality)
                    JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum)
                    = ARRAY[{columns}]::text[]
                AND NOT EXISTS (
                    SELECT 1 FROM unnest(i.indkey) WITH ORDINALITY k(attnum,ordinality)
                    JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
                    JOIN pg_opclass opc ON opc.oid=i.indclass[k.ordinality-1]
                    LEFT JOIN pg_collation coll ON coll.oid=i.indcollation[k.ordinality-1]
                    WHERE k.ordinality<=i.indnkeyatts
                        AND (NOT opc.opcdefault OR i.indcollation[k.ordinality-1]<>a.attcollation)))
        THEN RAISE EXCEPTION '{name} invalid or definition differs; preserve and inspect';
        END IF;
        END $$"""


def upgrade():
    for spec in INDEXES:
        op.execute(_guard(spec, required=False))
    # Each build commits independently. A failed/interrupted index is preserved
    # for explicit inspection; a same-name different definition is never reused.
    for spec in reversed(INDEXES):
        name, table, keys, included, options = spec
        ordered = ", ".join(key + (" DESC" if option == 3 else "") for key, option in zip(keys, options))
        with op.get_context().autocommit_block():
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} ({ordered}) "
                       f"INCLUDE ({', '.join(included)})")
        op.execute(_guard(spec, required=True))


def downgrade():
    with op.get_context().autocommit_block():
        for name, *_ in reversed(INDEXES):
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS " + name)
