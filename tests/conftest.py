"""Общая обвязка тестов: чистая база с демонстрационным набором."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def _isolated_db(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Своя база и выключенная LLM: тесты проверяют эвристики, а не сеть."""
    db = tmp_path_factory.mktemp("db") / "test.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db.as_posix()}"
    os.environ["LLM_PROVIDER"] = "none"
    os.environ["ASR_PROVIDER"] = "none"

    from app.config import get_settings
    from app.db import reset_state

    get_settings.cache_clear()
    reset_state()


# Момент «сейчас» для тестов. К 10 октября 2026 все отложенные события
# сентября уже наступили: и срок возврата долей по общему счёту, и срок,
# после которого нераспределённые наличные признаются тратой.
NOW = datetime(2026, 10, 10)

DEMO_FILES = (
    ("tbank_2026-07_09.csv", "IMPORT"),
    ("alfa_2026-09.csv", "IMPORT"),
    ("wildberries_2026-09.csv", "IMPORT"),
    ("cash_voice_2026-09.csv", "VOICE"),
)


def seed_user(tg_id: int, *, now: datetime = NOW) -> int:
    """Заводит пользователя и заливает ему демонстрационный набор.

    Отдельный пользователь на каждый файл тестов: классификация идёт по
    пользователю, и тест, отвечающий на вопрос, не должен ломать соседний
    тест, который проверяет исходное состояние.
    """
    from app.classify.pipeline import reclassify
    from app.db import init_db, session_scope
    from app.ingest.base import parse_statement
    from app.ingest.service import import_rows
    from app.models import OperationSource, User
    from scripts.gen_dataset import build

    build()
    init_db()

    demo = ROOT / "data" / "demo"
    with session_scope() as session:
        user = User(tg_id=tg_id, display_name="Сергей К.")
        session.add(user)
        session.flush()
        for name, origin in DEMO_FILES:
            bank, rows = parse_statement((demo / name).read_bytes(), filename=name)
            import_rows(
                session, user.id, rows, source=bank, filename=name,
                origin=OperationSource[origin],
            )
        reclassify(session, user.id, now=now)
        return user.id


@pytest.fixture(scope="session")
def seeded(_isolated_db: None) -> int:
    return seed_user(777)


@pytest.fixture
def session():
    from app.db import session_scope

    with session_scope() as db:
        yield db
