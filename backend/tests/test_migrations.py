"""Schema migrations (app/migrate.py, app/migrations): on SQLite and on real Postgres."""

from __future__ import annotations

import shutil
import textwrap
import threading
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlmodel import SQLModel

from app import db, migrate
from app.config import reload_settings, settings
from app.migrations.filters import include_name, include_object

MIGRATIONS = Path(migrate.__file__).parent / "migrations"


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(params=["sqlite", "postgres"])
def fresh_db(request, tmp_path, monkeypatch):
    """An empty database of each kind, set as the app's database."""
    monkeypatch.setenv("AHQ_DATA_DIR", str(tmp_path))
    if request.param == "sqlite":
        monkeypatch.setenv("AHQ_DB_URL", "")
    else:
        base = request.getfixturevalue("postgres_url")
        name = f"mig_{uuid.uuid4().hex[:10]}"
        admin = create_engine(base.replace("postgresql://", "postgresql+psycopg://"),
                              isolation_level="AUTOCOMMIT")
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        admin.dispose()
        monkeypatch.setenv("AHQ_DB_URL", base.rsplit("/", 1)[0] + "/" + name)
    for var in ("AHQ_MIGRATE_ON_START", "AHQ_ALLOW_SCHEMA_AHEAD", "AHQ_MIGRATE_WAIT_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    reload_settings()
    db.reset_engine()
    yield request.param
    db.reset_engine()


def _diffs():
    with db.engine().connect() as conn:
        mc = MigrationContext.configure(conn, opts={
            "compare_type": True, "include_name": include_name, "include_object": include_object,
            "target_metadata": SQLModel.metadata})
        return compare_metadata(mc, SQLModel.metadata)


def _versions() -> list[str]:
    with db.engine().connect() as c:
        return [r[0] for r in c.execute(text("SELECT version_num FROM alembic_version"))]


# --------------------------------------------------------------------------- the revisions


def test_revisions_build_the_models_schema(fresh_db):
    """Replaying every revision on an empty database (what startup does) gives exactly the
    models' schema.

    Fails when a model changed without a revision: run `python -m app.migrate revision -m ...`.
    """
    db.init_db()
    assert _versions() == [migrate.head()]
    assert _diffs() == []


def test_single_head_and_baseline():
    assert migrate.head()  # raises on several heads
    assert migrate.known(migrate.BASELINE)
    assert not migrate.known("ffff")


# --------------------------------------------------------------------------- startup


def test_fresh_database_is_created_and_stamped(fresh_db):
    db.init_db()
    db.init_db()  # and again: nothing to do
    assert migrate.current() == migrate.head()
    assert _versions() == [migrate.head()]
    assert _diffs() == []


def test_database_from_before_migrations_is_adopted(fresh_db):
    SQLModel.metadata.create_all(db.engine())
    with db.engine().begin() as c:
        c.execute(text("INSERT INTO skill (id, name, title, when_to_use, body, tools, scope, status, "
                       "version, created_by, created_at, updated_at) VALUES ('s1', 'probe', 'Probe', "
                       "'', 'steps', '[]', 'team', 'active', 1, '', '2026-01-01', '2026-01-01')"))
        c.execute(text('ALTER TABLE skill DROP COLUMN "script"'))  # what an older version lacked
    assert migrate.current() is None
    db.init_db()
    assert migrate.current() == migrate.BASELINE
    assert "script" in {c["name"] for c in inspect(db.engine()).get_columns("skill")}
    with db.engine().connect() as c:
        assert c.execute(text("SELECT body FROM skill WHERE id = 's1'")).scalar() == "steps"
    assert _diffs() == []
    if fresh_db == "sqlite":
        assert list((Path(settings.data_dir) / "backups").glob("agent_hq-legacy-*.db"))


def test_old_database_needs_the_baseline_release_once():
    with pytest.raises(migrate.SchemaError, match="introduced them"):
        migrate._adopt_legacy("0002")


def test_database_ahead_of_this_code_refuses(fresh_db, monkeypatch):
    db.init_db()
    with db.engine().begin() as c:
        c.execute(text("UPDATE alembic_version SET version_num = 'ffff'"))
    with pytest.raises(migrate.SchemaError, match="ffff"):
        db.init_db()
    monkeypatch.setenv("AHQ_ALLOW_SCHEMA_AHEAD", "1")
    reload_settings()
    db.init_db()  # starts, with a warning
    assert migrate.current() == "ffff"


def test_no_migrate_on_start_waits_then_gives_up(fresh_db, monkeypatch):
    monkeypatch.setenv("AHQ_MIGRATE_ON_START", "0")
    monkeypatch.setenv("AHQ_MIGRATE_WAIT_SECONDS", "0.1")
    reload_settings()
    with pytest.raises(migrate.SchemaError, match="MIGRATE_ON_START=0"):
        db.init_db()
    assert not inspect(db.engine()).get_table_names()  # it changed nothing

    monkeypatch.setenv("AHQ_MIGRATE_ON_START", "1")
    reload_settings()
    db.init_db()
    monkeypatch.setenv("AHQ_MIGRATE_ON_START", "0")
    reload_settings()
    db.init_db()  # at head: starts at once


def test_processes_starting_together_migrate_once(fresh_db):
    errors: list[BaseException] = []

    def start():
        try:
            migrate.ensure_schema()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=start) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    assert _versions() == [migrate.head()]
    assert _diffs() == []


def test_runtime_columns_and_foreign_tables_are_left_alone(fresh_db):
    """pgvector columns, FTS indexes and LangGraph's tables aren't in the models; autogenerate
    must not propose dropping them."""
    if fresh_db != "postgres":
        pytest.skip("these only exist on Postgres")
    db.init_db()
    with db.engine().begin() as c:
        c.execute(text("ALTER TABLE memory ADD COLUMN IF NOT EXISTS embedding_vec text"))
        c.execute(text("ALTER TABLE knowledgechunk ADD COLUMN IF NOT EXISTS embedding_vec text"))
        c.execute(text("CREATE INDEX IF NOT EXISTS memory_fts ON memory "
                       "USING gin (to_tsvector('english', text))"))
        c.execute(text("CREATE TABLE IF NOT EXISTS checkpoints (thread_id text PRIMARY KEY)"))
    assert _diffs() == []


# --------------------------------------------------------------------------- a later revision


RENAME = '''
"""rename skill.when_to_use to skill.use_when and tidy titles"""
from alembic import op
import sqlalchemy as sa

revision = "0099"
down_revision = "{down}"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("skill") as b:
        b.alter_column("when_to_use", new_column_name="use_when")
    skill = sa.table("skill", sa.column("title", sa.String))
    op.execute(skill.update().values(title=sa.func.upper(skill.c.title)))


def downgrade():
    raise NotImplementedError
'''


def test_a_rename_with_data_runs_on_startup(fresh_db, tmp_path, monkeypatch):
    """A revision after the baseline: rename a column and change data, on both databases,
    through the normal startup path (with a SQLite backup first)."""
    scripts = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    (scripts / "versions" / "0099_rename.py").write_text(
        textwrap.dedent(RENAME).format(down=migrate.head()))
    db.init_db()  # at today's head
    with db.engine().begin() as c:
        c.execute(text("INSERT INTO skill (id, name, title, when_to_use, body, tools, scope, status, "
                       "version, created_by, created_at, updated_at) VALUES ('s1', 'probe', 'Probe', "
                       "'when the NAS is slow', '', '[]', 'team', 'active', 1, '', '2026-01-01', "
                       "'2026-01-01')"))

    real_config = migrate.config

    def config(connection=None):
        cfg = real_config(connection)
        cfg.set_main_option("script_location", str(scripts))
        return cfg

    monkeypatch.setattr(migrate, "config", config)
    assert migrate.head() == "0099"
    db.init_db()
    assert migrate.current() == "0099"
    with db.engine().connect() as c:
        row = c.execute(text("SELECT title, use_when FROM skill WHERE id = 's1'")).one()
    assert row == ("PROBE", "when the NAS is slow")
    if fresh_db == "sqlite":
        assert list((Path(settings.data_dir) / "backups").glob(f"agent_hq-{migrate.BASELINE}-*.db"))


def test_new_not_null_columns_get_a_database_default():
    """Autogenerate gives an added NOT NULL column the model's default as a database default,
    so the revision works on tables that already have rows."""
    from alembic.operations import ops
    from sqlalchemy import Column

    from app import models
    from app.migrations.filters import process_revision_directives

    t = models.Skill.__table__
    added = [ops.AddColumnOp("skill", Column(c.name, c.type, nullable=c.nullable, default=c.default.arg
                                             if c.default is not None and c.default.is_scalar else None))
             for c in (t.c.title, t.c.version, t.c.scope, t.c.created_at)]
    script = ops.MigrationScript("x", ops.UpgradeOps(ops=[ops.ModifyTableOps("skill", added)]),
                                 ops.DowngradeOps(ops=[]))
    process_revision_directives(None, None, [script])
    got = {op.column.name: op.column.server_default.arg if op.column.server_default is not None else None
           for op in added}
    assert got == {"title": "", "version": "1", "scope": "team", "created_at": None}
