"""workbench egress rules

Per-workbench rules for the egress proxy (resource.egress_rules) and the token each
workbench signs in to it with (resource.egress_token).

Revision ID: 0005
Revises: 0004
Created: 2026-11-17 12:00:00

Rules (see docs/schema-migrations.md): never import app.models here; use sa.table() /
sa.column() stubs for data changes.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401
from alembic import op


revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
# Only adds columns (see migrate.additive_since_baseline).
additive = True


def upgrade() -> None:
    with op.batch_alter_table('resource', schema=None) as batch_op:
        batch_op.add_column(sa.Column('egress_rules', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('egress_token', sqlmodel.sql.sqltypes.AutoString(),
                                      server_default='', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('resource', schema=None) as batch_op:
        batch_op.drop_column('egress_token')
        batch_op.drop_column('egress_rules')
