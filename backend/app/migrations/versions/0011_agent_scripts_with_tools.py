"""scripts with tools

scripts_with_tools on agents: offer run_with_tools, a Python script on the agent's workbench
that calls its other tools through the same approval rules (app/runtime.py).

Revision ID: 0011
Revises: 0010
Created: 2026-11-19 22:30:00

Rules (see docs/schema-migrations.md): never import app.models here; use sa.table() /
sa.column() stubs for data changes.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401
from alembic import op


revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
# Only adds columns (see migrate.additive_since_baseline).
additive = True


def upgrade() -> None:
    with op.batch_alter_table('agentdef', schema=None) as batch_op:
        batch_op.add_column(sa.Column('scripts_with_tools', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('agentdef', schema=None) as batch_op:
        batch_op.drop_column('scripts_with_tools')
