"""agent next actions

next_actions on agents: auto | always | off. How the agent breaks a request into next
actions it works one at a time (app/nextactions.py). Existing agents get auto.

Revision ID: 0015
Revises: 0014
Created: 2026-11-24 08:00:00

Rules (see docs/schema-migrations.md): never import app.models here; use sa.table() /
sa.column() stubs for data changes.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401
from alembic import op


revision: str = '0015'
down_revision: str | None = '0014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
# Only adds columns (see migrate.additive_since_baseline).
additive = True


def upgrade() -> None:
    with op.batch_alter_table('agentdef', schema=None) as batch_op:
        batch_op.add_column(sa.Column('next_actions', sqlmodel.sql.sqltypes.AutoString(), nullable=False,
                                      server_default='auto'))


def downgrade() -> None:
    with op.batch_alter_table('agentdef', schema=None) as batch_op:
        batch_op.drop_column('next_actions')
