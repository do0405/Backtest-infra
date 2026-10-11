"""Widen explicit security aliases and index their immutable evidence keys."""
from alembic import op
import sqlalchemy as sa

revision = "6c347adb8181"
down_revision = "05d5349a9e80"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("identifier_history", "identifier", existing_type=sa.String(120),
                    type_=sa.String(500), existing_nullable=False)
    op.create_index("ix_identifier_history_alias_normalized_key", "identifier_history",
                    [sa.text("md5(evidence ->> 'normalized_key')")])
    op.create_index("ix_identifier_history_alias_assertion_key", "identifier_history",
                    [sa.text("(evidence ->> 'assertion_key')")])


def downgrade():
    op.drop_index("ix_identifier_history_alias_assertion_key", table_name="identifier_history")
    op.drop_index("ix_identifier_history_alias_normalized_key", table_name="identifier_history")
    # Preserve names on downgrade; refusing an unsafe narrowing is preferable to
    # truncating immutable evidence. PostgreSQL rejects values above the old bound.
    op.alter_column("identifier_history", "identifier", existing_type=sa.String(500),
                    type_=sa.String(120), existing_nullable=False)
