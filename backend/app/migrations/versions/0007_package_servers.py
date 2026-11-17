"""package servers

Resources of kind "package": an MCP server from the catalog, published as an npm or PyPI
package, run in a container of its own (mcp-runner/). resource.package holds what it
runs: registry, identifier, version, arguments, environment and its bearer token.

Revision ID: 0007
Revises: 0006
Created: 2026-11-17 14:00:00

Rules (see docs/schema-migrations.md): never import app.models here; use sa.table() /
sa.column() stubs for data changes.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401
from alembic import op


revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
# Only adds columns (see migrate.additive_since_baseline).
additive = True


def upgrade() -> None:
    with op.batch_alter_table('resource', schema=None) as batch_op:
        batch_op.add_column(sa.Column('package', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('resource', schema=None) as batch_op:
        batch_op.drop_column('package')
