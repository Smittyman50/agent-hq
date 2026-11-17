"""tool server headers and catalog origin

HTTP headers for tool servers (toolserver.headers, values encrypted with AHQ_SECRET_KEY)
and where a server came from in the MCP server catalog (catalog_name, catalog_version).

Revision ID: 0006
Revises: 0005
Created: 2026-11-17 13:00:00

Rules (see docs/schema-migrations.md): never import app.models here; use sa.table() /
sa.column() stubs for data changes.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401
from alembic import op


revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
# Only adds columns (see migrate.additive_since_baseline).
additive = True


def upgrade() -> None:
    with op.batch_alter_table('toolserver', schema=None) as batch_op:
        batch_op.add_column(sa.Column('headers', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('catalog_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column('catalog_version', sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('toolserver', schema=None) as batch_op:
        batch_op.drop_column('catalog_version')
        batch_op.drop_column('catalog_name')
        batch_op.drop_column('headers')
