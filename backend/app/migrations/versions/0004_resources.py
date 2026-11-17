"""resources: desktops and workbenches managed from the UI

A Resource table (one container per desktop or workbench, dedicated to an agent or
shared), the tool servers each one owns (toolserver.resource_id), and tool servers every
agent gets without ticking them (toolserver.all_agents).

Revision ID: 0004
Revises: 0003
Created: 2026-11-17 08:30:00

Rules (see docs/schema-migrations.md): never import app.models here; use sa.table() /
sa.column() stubs for data changes.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401
from alembic import op


revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
# Only adds a table, columns and an index (see migrate.additive_since_baseline).
additive = True


def upgrade() -> None:
    op.create_table(
        'resource',
        sa.Column('id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('kind', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('scope', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('managed', sa.Boolean(), nullable=False),
        sa.Column('container', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('network_access', sa.Boolean(), nullable=False),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column('updated_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('resource', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_resource_name'), ['name'], unique=True)

    with op.batch_alter_table('toolserver', schema=None) as batch_op:
        batch_op.add_column(sa.Column('all_agents', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('resource_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.create_index(batch_op.f('ix_toolserver_resource_id'), ['resource_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('toolserver', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_toolserver_resource_id'))
        batch_op.drop_column('resource_id')
        batch_op.drop_column('all_agents')

    with op.batch_alter_table('resource', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_resource_name'))
    op.drop_table('resource')
