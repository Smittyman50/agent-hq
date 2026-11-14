"""What autogenerate compares: everything in the models, nothing that is managed elsewhere."""

from __future__ import annotations

# Not ours to manage: LangGraph's checkpointer keeps its tables in the same Postgres
# database and migrates them itself.
IGNORE_TABLES = {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations",
                 "alembic_version"}
# Added at runtime by memory.init_storage() / knowledge.init_storage() when pgvector is
# available, so they are not in the models. Autogenerate must not propose dropping them.
RUNTIME_COLUMNS = {("memory", "embedding_vec"), ("knowledgechunk", "embedding_vec")}
RUNTIME_INDEXES = {"memory_fts", "knowledgechunk_fts"}


def include_name(name, type_, parent_names):
    if type_ == "table":
        return name not in IGNORE_TABLES
    if type_ == "index":
        return name not in RUNTIME_INDEXES
    return True


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "column" and reflected and compare_to is None:
        return (obj.table.name, name) not in RUNTIME_COLUMNS
    if type_ == "index" and name in RUNTIME_INDEXES:
        return False
    return True


# --------------------------------------------------------------------------- new columns


def _server_default(col):
    """The model's plain default as a database default, or None if it has none we can use.

    Most model fields are NOT NULL with a Python default (title: str = ""). Autogenerate
    adds such a column with no database default, which fails on a table that has rows; the
    old startup step back-filled the default instead. This gives the revision the same."""
    import enum
    import json

    d = col.default
    if d is None or not getattr(d, "is_scalar", False):
        return None  # none, or a callable (default_factory): write the back-fill by hand
    v = d.arg
    if isinstance(v, enum.Enum):
        return v.name  # SQLAlchemy stores enum names
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (str, int, float)):
        return str(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return None


def process_revision_directives(context, revision, directives):
    """Autogenerate hook: NOT NULL columns added to existing tables get a database default."""
    from alembic.operations import ops

    def walk(op):
        for sub in getattr(op, "ops", []):
            walk(sub)
        if isinstance(op, ops.AddColumnOp):
            col = op.column
            if not col.nullable and col.server_default is None:
                value = _server_default(col)
                if value is not None:
                    from sqlalchemy import DefaultClause

                    col.server_default = DefaultClause(value)

    for d in directives:
        walk(d.upgrade_ops)
