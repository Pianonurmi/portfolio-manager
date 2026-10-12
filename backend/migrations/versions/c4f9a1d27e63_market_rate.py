"""market rate

Revision ID: c4f9a1d27e63
Revises: b3e8d2f6a914
Create Date: 2026-10-12 18:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'c4f9a1d27e63'
down_revision = 'b3e8d2f6a914'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'market_rate',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('series', sa.String(length=96), nullable=False),
        sa.Column('label', sa.String(length=160), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('value', sa.Float(), nullable=False),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('series', 'date', name='uq_market_rate_series_date'),
    )


def downgrade() -> None:
    op.drop_table('market_rate')
