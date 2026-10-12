"""instrument profiles

Revision ID: b7d41c0a93e2
Revises: 5ac168aab864
Create Date: 2026-10-12 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'b7d41c0a93e2'
down_revision = '5ac168aab864'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('instrument_profile',
    sa.Column('instrument_id', sa.BigInteger(), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('as_of', sa.Date(), nullable=True),
    sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.ForeignKeyConstraint(['instrument_id'], ['instrument.id'], name=op.f('fk_instrument_profile_instrument_id_instrument'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_instrument_profile')),
    sa.UniqueConstraint('instrument_id', 'source', name=op.f('uq_instrument_profile_instrument_id'))
    )


def downgrade() -> None:
    op.drop_table('instrument_profile')
