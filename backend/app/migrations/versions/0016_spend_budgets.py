"""spend budgets

Budgets on the usage ledger (Reports → Budgets; app/budgets.py): a limit per agent or per
provider for a day, week or month, on requests, tokens, local run time or estimated cost.
They only warn.

Revision ID: 0016
Revises: 0015
Created: 2026-11-24 21:30:00

Rules (see docs/schema-migrations.md): never import app.models here; use sa.table() /
sa.column() stubs for data changes.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401
from alembic import op


revision: str = '0016'
down_revision: str | None = '0015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
# Only adds a table and its indexes (see migrate.additive_since_baseline).
additive = True


def upgrade() -> None:
    op.create_table('spendbudget',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('scope', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('target_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('period', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('metric', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('limit', sa.Float(), nullable=False),
    sa.Column('warn_pct', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('state', sa.JSON(), nullable=True),
    sa.Column('created_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('spendbudget', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_spendbudget_scope'), ['scope'], unique=False)
        batch_op.create_index(batch_op.f('ix_spendbudget_target_id'), ['target_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('spendbudget', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_spendbudget_target_id'))
        batch_op.drop_index(batch_op.f('ix_spendbudget_scope'))
    op.drop_table('spendbudget')
