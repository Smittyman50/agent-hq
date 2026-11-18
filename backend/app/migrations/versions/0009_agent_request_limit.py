"""agent request limit

max_requests on agents: the most model requests one run may make (None = 50).

Revision ID: 0009
Revises: 0008
Created: 2026-11-18 11:30:00

Rules (see docs/schema-migrations.md): never import app.models here; use sa.table() /
sa.column() stubs for data changes.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401
from alembic import op


revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
# Only adds columns (see migrate.additive_since_baseline).
additive = True


def upgrade() -> None:
    with op.batch_alter_table('agentdef', schema=None) as batch_op:
        batch_op.add_column(sa.Column('max_requests', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('agentdef', schema=None) as batch_op:
        batch_op.drop_column('max_requests')
