"""Separate provider IPO/delisting dates from snapshot and publication clocks."""
from alembic import op
import sqlalchemy as sa
revision='c71302d6b908'
down_revision='c71302d6b907'
branch_labels=None
depends_on=None
def upgrade():
    op.add_column('supplemental_observation',sa.Column('ipo_date',sa.Date(),nullable=True))
    op.add_column('supplemental_observation',sa.Column('delisting_date',sa.Date(),nullable=True))
def downgrade():
    op.drop_column('supplemental_observation','delisting_date')
    op.drop_column('supplemental_observation','ipo_date')
