"""etf listing brokers

Revision ID: b3e8d2f6a914
Revises: a7c3e9d15b42
Create Date: 2026-10-12 16:05:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'b3e8d2f6a914'
down_revision = 'a7c3e9d15b42'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('etf_listing', sa.Column('brokers', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False))


def downgrade() -> None:
    op.drop_column('etf_listing', 'brokers')
