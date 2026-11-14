"""Alembic environment for Agent HQ.

Run through app/migrate.py (startup, `python -m app.migrate`), which passes the app's own
connection in config.attributes["connection"]. Without one (the plain alembic CLI) the app's
engine is used, so the database is always the one AHQ_DB_URL / AHQ_DATA_DIR point at.
"""

from __future__ import annotations

from alembic import context
from sqlmodel import SQLModel

from app import models  # noqa: F401  (register tables)
from app.migrations.filters import include_name, include_object, process_revision_directives

config = context.config
target_metadata = SQLModel.metadata

def _configure(**kw) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        include_name=include_name,
        include_object=include_object,
        process_revision_directives=process_revision_directives,
        **kw,
    )


def run_migrations_offline() -> None:
    """`alembic upgrade --sql`: print the SQL instead of running it."""
    from app.config import settings

    url = settings.db_url
    _configure(url=url, literal_binds=True, render_as_batch=url.startswith("sqlite"))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is None:
        from app import db

        with db.engine().connect() as connection:
            _run(connection)
    else:
        _run(connection)


def _run(connection) -> None:
    # SQLite can't alter or rename columns in place; batch mode rebuilds the table instead.
    _configure(connection=connection, render_as_batch=connection.dialect.name == "sqlite")
    with context.begin_transaction():
        context.run_migrations()
    if connection.in_transaction():
        connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
