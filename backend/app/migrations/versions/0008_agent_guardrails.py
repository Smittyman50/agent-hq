"""agent guardrails

Per-agent limits for its main model: max_output_tokens (most tokens one response may
generate, thinking included) and request_timeout_s (deadline for one model request).

Revision ID: 0008
Revises: 0007
Created: 2026-11-18 07:00:00

Rules (see docs/schema-migrations.md): never import app.models here; use sa.table() /
sa.column() stubs for data changes.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401
from alembic import op


revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
# Only adds columns (see migrate.additive_since_baseline).
additive = True


def upgrade() -> None:
    with op.batch_alter_table('agentdef', schema=None) as batch_op:
        batch_op.add_column(sa.Column('max_output_tokens', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('request_timeout_s', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('agentdef', schema=None) as batch_op:
        batch_op.drop_column('request_timeout_s')
        batch_op.drop_column('max_output_tokens')
