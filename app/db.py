"""Подключение к БД. Синхронный SQLAlchemy.

Почему синхронный: вся содержательная логика (классификация, леджер,
аналитика) — обычный тестируемый Python без async-заразы. FastAPI сам
уводит sync-эндпоинты в тредпул, aiogram-хендлеры вызывают через
asyncio.to_thread. На SQLite это быстрее, чем накладные расходы на async.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _configure_sqlite(engine: Engine) -> None:
    """WAL + внешние ключи. Без foreign_keys=ON SQLite их просто игнорирует."""

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        path = settings.db_path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            settings.database_url,
            future=True,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False}
            if settings.database_url.startswith("sqlite")
            else {},
        )
        if settings.database_url.startswith("sqlite"):
            _configure_sqlite(_engine)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Транзакция как контекстный менеджер."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Создаёт схему. Для хакатона достаточно, Alembic подключается сверху."""
    Base.metadata.create_all(get_engine())


def reset_state() -> None:
    """Сбрасывает кешированный engine — нужно тестам, меняющим DATABASE_URL."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
