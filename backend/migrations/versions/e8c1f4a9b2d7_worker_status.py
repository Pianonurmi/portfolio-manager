"""worker status

Revision ID: e8c1f4a9b2d7
Revises: d7a2c5e81f30
Create Date: 2026-10-12 20:15:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "e8c1f4a9b2d7"
down_revision = "d7a2c5e81f30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_status",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker_status")),
    )


def downgrade() -> None:
    op.drop_table("worker_status")
