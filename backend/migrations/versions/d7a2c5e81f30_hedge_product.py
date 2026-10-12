"""hedge product

Revision ID: d7a2c5e81f30
Revises: c4f9a1d27e63
Create Date: 2026-10-12 18:20:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'd7a2c5e81f30'
down_revision = 'c4f9a1d27e63'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('hedge_product',
    sa.Column('isin', sa.String(length=12), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('direction', sa.String(length=8), nullable=False),
    sa.Column('index_key', sa.String(length=16), nullable=False),
    sa.Column('leverage', sa.Float(), nullable=True),
    sa.Column('financing_level', sa.Float(), nullable=True),
    sa.Column('barrier', sa.Float(), nullable=True),
    sa.Column('currency', sa.String(length=8), nullable=True),
    sa.Column('issuer', sa.String(length=64), nullable=True),
    sa.Column('nordnet_id', sa.BigInteger(), nullable=True),
    sa.Column('seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_hedge_product')),
    sa.UniqueConstraint('isin', name=op.f('uq_hedge_product_isin'))
    )
    op.create_index('ix_hedge_product_index', 'hedge_product', ['index_key', 'kind'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_hedge_product_index', table_name='hedge_product')
    op.drop_table('hedge_product')
