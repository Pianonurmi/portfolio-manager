"""price.updated_at: lets the in-memory price snapshot notice closes changed in place

Revision ID: c2a9e7d41f05
Revises: b7d41c0a93e2
Create Date: 2026-10-11 23:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'c2a9e7d41f05'
down_revision = 'b7d41c0a93e2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('price', sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False))


def downgrade() -> None:
    op.drop_column('price', 'updated_at')
