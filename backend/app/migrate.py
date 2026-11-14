"""Schema migrations (Alembic), run on startup by db.init_db() and by hand with
`python -m app.migrate`.

Every process that opens the database (API, workers, the account CLI) calls
ensure_schema(). Under a lock, so that processes starting together don't race, it:

- empty database: runs every revision (the baseline creates all the tables);
- database from before Alembic (tables, no alembic_version): adds the columns that
  version lacked, the old way, then stamps it at the baseline revision;
- versioned database behind head: backs it up (SQLite) and upgrades it;
- database at a revision this code doesn't know (a newer version migrated it): refuses
  to start, unless AHQ_ALLOW_SCHEMA_AHEAD=1.

With AHQ_MIGRATE_ON_START=0 the process never changes the schema; it waits for another
process to bring the database to its head. See docs/schema-migrations.md.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

# Alembic logs its plugins and dialect on every connection; our own lines say what ran.
for _name in ("alembic.runtime.plugins", "alembic.runtime.migration"):
    logging.getLogger(_name).setLevel(logging.WARNING)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.runtime.migration import MigrationContext  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from alembic.util.exc import CommandError  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402

from . import db  # noqa: E402
from .config import settings  # noqa: E402

log = logging.getLogger("agent_hq.migrate")

BASELINE = "0001"  # the schema as it was when migrations were introduced
LOCK_KEY = 0x4148_515F_4D49_4752 & 0x7FFF_FFFF_FFFF_FFFF  # "AHQ_MIGR", a Postgres advisory lock id
KEEP_BACKUPS = 5


class SchemaError(RuntimeError):
    """The database can't be used by this version of Agent HQ (message says why and what to do)."""


# --------------------------------------------------------------------------- alembic plumbing


def config(connection=None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", "app:migrations")
    cfg.set_main_option("file_template", "%%(rev)s_%%(slug)s")
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def script() -> ScriptDirectory:
    return ScriptDirectory.from_config(config())


def head() -> str:
    heads = script().get_heads()
    if len(heads) != 1:
        raise SchemaError(f"migrations have {len(heads)} heads ({', '.join(heads)}); "
                          "merge them with `alembic merge` before starting")
    return heads[0]


def known(rev: str) -> bool:
    try:
        return script().get_revision(rev) is not None
    except CommandError:
        return False


def current() -> str | None:
    with db.engine().connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _app_tables() -> set[str]:
    return set(inspect(db.engine()).get_table_names()) & set(SQLModel.metadata.tables)


def _run(fn, *args, **kw) -> None:
    """Run an alembic command on one of the app's connections."""
    with db.engine().connect() as conn:
        fn(config(conn), *args, **kw)
        if conn.in_transaction():
            conn.commit()


# --------------------------------------------------------------------------- lock


@contextlib.contextmanager
def lock() -> Iterator[None]:
    """One process at a time: a Postgres advisory lock, or a lock file next to SQLite."""
    eng = db.engine()
    if eng.dialect.name == "postgresql":
        with eng.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": LOCK_KEY})
            conn.commit()
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
                conn.commit()
        return
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with open(settings.data_dir / ".migrate.lock", "a+b") as f:
        if sys.platform == "win32":
            import msvcrt

            f.seek(0)
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:  # LK_LOCK gives up after ~10 s; keep waiting
                    continue
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


# --------------------------------------------------------------------------- backups


def _sqlite_path() -> Path | None:
    eng = db.engine()
    if eng.dialect.name != "sqlite" or not eng.url.database or eng.url.database == ":memory:":
        return None
    return Path(eng.url.database)


def backup_sqlite(rev: str | None) -> Path | None:
    """Copy the SQLite database (online backup API) before changing it; keep the last few."""
    src = _sqlite_path()
    if src is None or not src.exists():
        return None
    folder = src.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = folder / f"{src.stem}-{rev or 'legacy'}-{stamp}{src.suffix}"
    with contextlib.closing(sqlite3.connect(src)) as a, contextlib.closing(sqlite3.connect(dest)) as b:
        a.backup(b)
    old = sorted(folder.glob(f"{src.stem}-*{src.suffix}"), key=lambda p: p.stat().st_mtime)
    for p in old[:-KEEP_BACKUPS]:
        with contextlib.suppress(OSError):
            p.unlink()
    log.info("database backed up to %s", dest)
    return dest


# --------------------------------------------------------------------------- startup


def _ahead(rev: str) -> bool:
    """True if the database is at a revision this code doesn't know. Raises unless allowed."""
    if known(rev):
        return False
    msg = (f"the database is at schema revision {rev}, which this version of Agent HQ doesn't "
           f"know (its newest is {head()}). A newer version has migrated it: update this "
           "process's image. Set AHQ_ALLOW_SCHEMA_AHEAD=1 to start anyway (only if you know "
           "the newer changes are compatible).")
    if settings.allow_schema_ahead:
        log.warning("%s Starting anyway (AHQ_ALLOW_SCHEMA_AHEAD=1).", msg)
        return True
    raise SchemaError(msg)


def ensure_schema() -> None:
    """Bring the database to this code's schema (or wait for it), as the module doc says."""
    target = head()
    if not settings.migrate_on_start:
        _wait_for(target)
        return
    with lock():
        rev = current()
        if rev is not None and _ahead(rev):
            return
        if rev == target:
            pass
        elif rev is None and not _app_tables():
            # Replay the revisions rather than create_all(): then the database matches the
            # revisions, not whatever the models say right now, and a model change without
            # a revision shows up (to autogenerate and to the tests) instead of hiding.
            _run(command.upgrade, "head")
            log.info("database created at schema revision %s", target)
        elif rev is None:
            _adopt_legacy(target)
        else:
            log.info("upgrading database from schema revision %s to %s", rev, target)
            backup_sqlite(rev)
            _run(command.upgrade, "head")
            log.info("database upgraded from schema revision %s to %s", rev, target)
        db.add_missing_enum_values()


def _adopt_legacy(target: str) -> None:
    """A database made before migrations existed: catch it up the old way, then stamp it."""
    if target != BASELINE:
        # The old catch-up compares with today's models, which no longer match the baseline
        # once later revisions exist (a rename would be added as a new empty column).
        raise SchemaError(
            "this database was created before schema migrations and needs the release that "
            f"introduced them (schema revision {BASELINE}) to start once first. Or, if you are "
            f"sure it matches that release's schema: python -m app.migrate stamp {BASELINE}")
    backup_sqlite(None)
    SQLModel.metadata.create_all(db.engine())  # tables that version didn't have yet
    db.add_missing_columns()
    _run(command.stamp, BASELINE)
    log.info("database from before migrations stamped at schema revision %s", BASELINE)


def _wait_for(target: str) -> None:
    deadline = time.monotonic() + settings.migrate_wait_seconds
    logged = False
    while True:
        rev = current()
        if rev == target or (rev is not None and _ahead(rev)):
            return
        if time.monotonic() >= deadline:
            raise SchemaError(
                f"the database is at schema revision {rev or 'none'}, not {target}, and "
                "AHQ_MIGRATE_ON_START=0, so this process won't migrate it. Start a process "
                "that migrates (hq), or run: python -m app.migrate upgrade")
        if not logged:
            log.info("waiting for the database to reach schema revision %s (now %s)", target, rev)
            logged = True
        time.sleep(2)


# --------------------------------------------------------------------------- CLI


def _next_rev_id() -> str:
    ids = [r.revision for r in script().walk_revisions()]
    nums = [int(i) for i in ids if i.isdigit()]
    return f"{max(nums, default=0) + 1:04d}"


def status() -> dict:
    rev, target = current(), head()
    pending: list[str] = []
    if rev is None or known(rev):
        for r in script().iterate_revisions(target, rev or "base"):
            pending.append(f"{r.revision} {r.doc or ''}".strip())
    return {"database": db.engine().url.render_as_string(hide_password=True), "current": rev,
            "head": target, "pending": list(reversed(pending)) if rev != target else []}


def main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(prog="python -m app.migrate", description="Agent HQ schema migrations")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="current revision, head, and what's pending")
    sub.add_parser("upgrade", help="bring the database to head now (same steps as startup)")
    rv = sub.add_parser("revision", help="new revision, autogenerated from the models")
    rv.add_argument("-m", "--message", required=True)
    rv.add_argument("--empty", action="store_true", help="don't autogenerate (for data-only changes)")
    st = sub.add_parser("stamp", help="mark the database as at a revision without running anything")
    st.add_argument("revision")
    sub.add_parser("history", help="list revisions")
    a = p.parse_args(argv)

    from . import models  # noqa: F401  (register tables)

    try:
        if a.cmd == "status":
            s = status()
            print(f"database: {s['database']}\ncurrent:  {s['current'] or 'none'}\nhead:     {s['head']}")
            for line in s["pending"]:
                print(f"pending:  {line}")
        elif a.cmd == "upgrade":
            settings.migrate_on_start = True
            ensure_schema()
            print(f"at {current()}")
        elif a.cmd == "revision":
            with lock():
                if current() != head():
                    print("bring the database to head first (python -m app.migrate upgrade): "
                          "autogenerate compares the models with the database as it is")
                    return 1
                _run(command.revision, message=a.message, autogenerate=not a.empty,
                     rev_id=_next_rev_id())
        elif a.cmd == "stamp":
            with lock():
                _run(command.stamp, a.revision)
            print(f"stamped {a.revision}")
        elif a.cmd == "history":
            for r in script().walk_revisions():
                print(f"{r.revision}  {r.doc or ''}")
    except SchemaError as e:
        print(f"error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
