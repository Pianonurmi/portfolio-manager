"""etf listing

Revision ID: a7c3e9d15b42
Revises: d4f1b8a2c6e9
Create Date: 2026-10-12 09:12:44.120331
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'a7c3e9d15b42'
down_revision = 'd4f1b8a2c6e9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('etf_listing',
    sa.Column('isin', sa.String(length=12), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('secid', sa.String(length=32), nullable=True),
    sa.Column('listings', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('provider', sa.String(length=128), nullable=True),
    sa.Column('category', sa.String(length=128), nullable=True),
    sa.Column('global_category', sa.String(length=128), nullable=True),
    sa.Column('ter', sa.Float(), nullable=True),
    sa.Column('fund_size_eur', sa.Float(), nullable=True),
    sa.Column('inception', sa.Date(), nullable=True),
    sa.Column('domicile', sa.String(length=8), nullable=True),
    sa.Column('distribution', sa.String(length=8), nullable=True),
    sa.Column('hedged', sa.Boolean(), nullable=False),
    sa.Column('base_currency', sa.String(length=8), nullable=True),
    sa.Column('equity_share', sa.Float(), nullable=True),
    sa.Column('duration', sa.Float(), nullable=True),
    sa.Column('credit_quality', sa.Integer(), nullable=True),
    sa.Column('srri', sa.Integer(), nullable=True),
    sa.Column('star_rating', sa.Integer(), nullable=True),
    sa.Column('sustainability', sa.Integer(), nullable=True),
    sa.Column('benchmark', sa.String(length=255), nullable=True),
    sa.Column('style_box', sa.String(length=32), nullable=True),
    sa.Column('returns', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('vol_3y', sa.Float(), nullable=True),
    sa.Column('sharpe_3y', sa.Float(), nullable=True),
    sa.Column('max_dd_3y', sa.Float(), nullable=True),
    sa.Column('yield_12m', sa.Float(), nullable=True),
    sa.Column('close_price', sa.Float(), nullable=True),
    sa.Column('close_currency', sa.String(length=8), nullable=True),
    sa.Column('seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_etf_listing')),
    sa.UniqueConstraint('isin', name=op.f('uq_etf_listing_isin'))
    )
    op.create_index('ix_etf_listing_name', 'etf_listing', ['name'], unique=False)
    op.create_index(op.f('ix_etf_listing_category'), 'etf_listing', ['category'], unique=False)
    op.create_index(op.f('ix_etf_listing_provider'), 'etf_listing', ['provider'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_etf_listing_provider'), table_name='etf_listing')
    op.drop_index(op.f('ix_etf_listing_category'), table_name='etf_listing')
    op.drop_index('ix_etf_listing_name', table_name='etf_listing')
    op.drop_table('etf_listing')
