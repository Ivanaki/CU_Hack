"""Подключение к БД. Синхронный SQLAlchemy.

Почему синхронный: вся содержательная логика (классификация, леджер,
аналитика) — обычный тестируемый Python без async-заразы. FastAPI сам
уводит sync-эндпоинты в тредпул, aiogram-хендлеры вызывают через
asyncio.to_thread. На SQLite это быстрее, чем накладные расходы на async.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Column, Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateColumn

from app.config import get_settings
from app.models import Base

log = logging.getLogger(__name__)

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


def _column_ddl(column: Column, engine: Engine) -> str | None:
    """Кусок DDL для ALTER TABLE ADD COLUMN. None — так добавить нельзя.

    Колонку NOT NULL без server_default SQLite в существующую таблицу не
    примет: старым строкам нечего в неё положить. Такую колонку добавлять
    молча нельзя, поэтому возвращаем None и говорим об этом в лог.
    """
    if not column.nullable and column.server_default is None:
        return None
    return str(CreateColumn(column).compile(dialect=engine.dialect))


def upgrade_schema(engine: Engine) -> None:
    """Догоняет схему уже существующей базы новыми колонками и индексами.

    create_all() создаёт только отсутствующие таблицы целиком и ничего не
    знает про ALTER TABLE, а база живёт в томе и переживает передеплой.
    Без этого шага добавленная колонка появилась бы только у того, кто
    начинает с нуля, а задеплоенный сервис падал бы на «no such column»
    при первом же запросе. Alembic для одного файла SQLite — лишний слой:
    перезапуск контейнера чинит базу сам.
    """
    inspector = inspect(engine)
    known = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in known:
            continue  # create_all уже создал её со всеми колонками
        have = {c["name"] for c in inspector.get_columns(table.name)}
        missing = [c for c in table.columns if c.name not in have]
        if not missing:
            continue
        with engine.begin() as conn:
            for column in missing:
                spec = _column_ddl(column, engine)
                if spec is None:
                    log.warning(
                        "колонку %s.%s нельзя добавить на ходу: нужен server_default",
                        table.name, column.name,
                    )
                    continue
                conn.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN {spec}')
                log.info("схема: добавлена колонка %s.%s", table.name, column.name)
            # Индексы создаются вместе с таблицей, поэтому у новой колонки
            # своего индекса нет — в том числе уникального, на котором
            # держится «один токен на одного человека».
            for index in table.indexes:
                index.create(conn, checkfirst=True)


def init_db() -> None:
    """Создаёт схему. Для хакатона достаточно, Alembic подключается сверху."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    upgrade_schema(engine)


def reset_state() -> None:
    """Сбрасывает кешированный engine — нужно тестам, меняющим DATABASE_URL."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
