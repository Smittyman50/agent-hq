"""automatic request time limit

request_timeout_auto on agents: each request to the main model gets a deadline worked out
from that model's measured speed (app/speed.py) instead of the fixed request_timeout_s.

Revision ID: 0010
Revises: 0009
Created: 2026-11-19 18:45:00

Rules (see docs/schema-migrations.md): never import app.models here; use sa.table() /
sa.column() stubs for data changes.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401
from alembic import op


revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
# Only adds columns (see migrate.additive_since_baseline).
additive = True


def upgrade() -> None:
    with op.batch_alter_table('agentdef', schema=None) as batch_op:
        batch_op.add_column(sa.Column('request_timeout_auto', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('agentdef', schema=None) as batch_op:
        batch_op.drop_column('request_timeout_auto')
