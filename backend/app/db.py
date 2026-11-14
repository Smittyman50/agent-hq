from __future__ import annotations

from collections.abc import Iterator

import enum
import json
import logging

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

_engine = None


def _make_engine(url: str):
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)


def engine():
    global _engine
    if _engine is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = _make_engine(settings.db_url)
    return _engine


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    from . import migrate

    migrate.ensure_schema()  # create, adopt or upgrade the schema (app/migrate.py)
    from . import memory

    memory.init_storage()
    from . import knowledge

    knowledge.init_storage()


log = logging.getLogger("agent_hq.db")


def _default_value(col):
    """A column's Python default as a plain value, or None if it has none we can use."""
    d = col.default
    if d is None:
        return None
    value = d.arg
    if callable(value):
        try:
            value = value(None) if getattr(d, "is_callable", False) else value()
        except TypeError:
            value = value()
    if isinstance(value, enum.Enum):
        value = value.name  # SQLAlchemy stores enum names
    return value


def add_missing_columns() -> None:
    """Add columns that newer versions introduced to tables that already exist.

    Only used to catch up a database made before schema migrations (migrate._adopt_legacy).
    Since then every schema change is an Alembic revision: this compares with the current
    models, so after a rename it would add the new name as an empty column.
    Columns are added as nullable and existing rows get the column's default.
    """
    eng = engine()
    insp = inspect(eng)
    existing_tables = set(insp.get_table_names())
    for table in SQLModel.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = col.type.compile(dialect=eng.dialect)
            with eng.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}'))
                value = _default_value(col)
                if value is not None:
                    if isinstance(value, (dict, list)):
                        value = json.dumps(value)
                    conn.execute(text(f'UPDATE "{table.name}" SET "{col.name}" = :v'), {"v": value})
            log.info("database upgraded: added %s.%s", table.name, col.name)


def add_missing_enum_values() -> None:
    """Add values that newer versions introduced to Postgres enum types (a new provider
    kind, say). create_all() makes an enum type once and never changes it, so without this
    saving a row with the new value fails on a database created by an older version."""
    eng = engine()
    if eng.dialect.name != "postgresql":
        return  # SQLite stores enums as plain text
    seen: set[str] = set()
    for table in SQLModel.metadata.sorted_tables:
        for col in table.columns:
            name = getattr(col.type, "name", None)
            values = getattr(col.type, "enums", None)
            if not name or not values or not getattr(col.type, "native_enum", False) or name in seen:
                continue
            seen.add(name)
            with eng.connect() as conn:
                have = {r[0] for r in conn.execute(text(
                    "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = :n"), {"n": name})}
            if not have:
                continue  # type not created yet (or named differently): nothing to upgrade
            for value in values:
                if value in have:
                    continue
                # ADD VALUE must commit before the new value can be used, so run it on its own
                with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                    conn.execute(text(f'ALTER TYPE "{name}" ADD VALUE IF NOT EXISTS \'{value}\''))
                log.info("database upgraded: added %s to enum %s", value, name)


def get_session() -> Iterator[Session]:
    with Session(engine()) as s:
        yield s


def session() -> Session:
    return Session(engine(), expire_on_commit=False)


def reset_engine(url: str | None = None) -> None:
    """Point at a different database (tests)."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = _make_engine(url or settings.db_url)
