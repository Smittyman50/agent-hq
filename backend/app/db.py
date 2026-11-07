from __future__ import annotations

from collections.abc import Iterator

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

    SQLModel.metadata.create_all(engine())
    from . import memory

    memory.init_storage()


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
