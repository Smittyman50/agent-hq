from __future__ import annotations

from collections.abc import Iterator

from sqlmodel import Session, SQLModel, create_engine

from .config import settings

_engine = None


def engine():
    global _engine
    if _engine is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(settings.db_url, connect_args={"check_same_thread": False}
                                if settings.db_url.startswith("sqlite") else {})
    return _engine


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    SQLModel.metadata.create_all(engine())


def get_session() -> Iterator[Session]:
    with Session(engine()) as s:
        yield s


def session() -> Session:
    return Session(engine(), expire_on_commit=False)


def reset_engine_for_tests(url: str) -> None:
    global _engine
    _engine = create_engine(url, connect_args={"check_same_thread": False})
