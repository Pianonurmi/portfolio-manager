"""distribution: dividends per unit by ex-date, for total-return maths

Revision ID: d4f1b8a3c960
Revises: c2a9e7d41f05
Create Date: 2026-10-12 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'd4f1b8a3c960'
down_revision = 'c2a9e7d41f05'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('distribution',
    sa.Column('instrument_id', sa.BigInteger(), nullable=False),
    sa.Column('ex_date', sa.Date(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=20, scale=8), nullable=False),
    sa.Column('currency', sa.String(length=8), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['instrument_id'], ['instrument.id'], name=op.f('fk_distribution_instrument_id_instrument'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_distribution')),
    sa.UniqueConstraint('instrument_id', 'ex_date', 'source', name=op.f('uq_distribution_instrument_id'))
    )


def downgrade() -> None:
    op.drop_table('distribution')
