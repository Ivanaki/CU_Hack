"""Непонятные деньги обязаны иметь способ стать понятными.

Живой случай: открытых вопросов ноль, а осознанность 81%. В минусе четыре
похода в «Bejs-Klab» по 300–1100 ₽ — каждый по отдельности до порога крупной
траты не дотягивал, вопроса про них не задавалось, и починить метрику было
нечем. Метрика, которую нельзя починить действием, хуже отсутствия метрики.

Главная проверка файла — последняя: ответили на все вопросы, и неясных денег
не осталось совсем, а осознанность стала ровно 100%.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from tests.test_api import BOT_TOKEN, make_init_data

UNCLEAR_TG_ID = 555_301
SMALL_TG_ID = 555_302

# Два неопознанных места и один узнаваемый магазин. «Bejs-Klab» — тот самый
# случай: четыре мелкие траты, вместе 2 650 ₽.
STATEMENT = """Дата операции;Номер счета;Сумма;Валюта;Назначение платежа
03.09.2026;40817810900000005531;-300,00;RUB;Bejs-Klab
09.09.2026;40817810900000005531;-550,00;RUB;Bejs-Klab
16.09.2026;40817810900000005531;-700,00;RUB;Bejs-Klab
23.09.2026;40817810900000005531;-1100,00;RUB;Bejs-Klab
05.09.2026;40817810900000005531;-1200,00;RUB;Studio Kray
19.09.2026;40817810900000005531;-900,00;RUB;Studio Kray
11.09.2026;40817810900000005531;-2400,00;RUB;Пятёрочка
"""

# Мелочь: за месяц 50 ₽ в одном месте. Пока они входят в «Не разобрано»,
# человек должен иметь возможность объяснить и их.
SMALL = """Дата операции;Номер счета;Сумма;Валюта;Назначение платежа
07.09.2026;40817810900000005531;-50,00;RUB;Kiosk Uglovoj
"""

PERIOD = "2026-09"


def auth(tg_id: int = UNCLEAR_TG_ID) -> dict[str, str]:
    return {"X-Telegram-Init-Data": make_init_data(tg_id, token=BOT_TOKEN)}


def upload(client: TestClient, body: str, tg_id: int) -> None:
    response = client.post(
        "/api/v1/me/statements",
        files={"file": ("alfa_unclear.csv", body.encode("utf-8"), "text/csv")},
        headers=auth(tg_id),
    )
    assert response.status_code == 200, response.text


@pytest.fixture(scope="module")
def client(_isolated_db):
    """Два своих пользователя: у одного есть что объяснять, у другого мелочь."""
    import os

    os.environ["BOT_TOKEN"] = BOT_TOKEN

    from app.config import get_settings

    get_settings.cache_clear()
    from app.api.app import create_app

    with TestClient(create_app()) as test_client:
        upload(test_client, STATEMENT, UNCLEAR_TG_ID)
        upload(test_client, SMALL, SMALL_TG_ID)
        yield test_client
    get_settings.cache_clear()


def questions(client: TestClient, tg_id: int = UNCLEAR_TG_ID) -> list[dict]:
    response = client.get("/api/v1/me/questions?limit=100", headers=auth(tg_id))
    assert response.status_code == 200, response.text
    return response.json()


def summary(client: TestClient, tg_id: int = UNCLEAR_TG_ID) -> dict:
    response = client.get(f"/api/v1/me/summary?period={PERIOD}", headers=auth(tg_id))
    assert response.status_code == 200, response.text
    return response.json()


def answer(
    client: TestClient, question: dict, code: str, text: str = "", tg_id: int = UNCLEAR_TG_ID
):
    return client.post(
        f"/api/v1/me/questions/{question['id']}/answer",
        json={"code": code, "text": text},
        headers=auth(tg_id),
    )


def test_про_неопознанное_место_спрашивают_одним_вопросом(client):
    """Один вопрос на продавца, а не по вопросу на каждую трату.

    Девяносто девять одинаковых вопросов человек закрывает не ответами, а
    выходом из приложения.
    """
    about = [q for q in questions(client) if q["kind"] == "unknown_place"]
    assert len(about) == 2  # Bejs-Klab и Studio Kray, каждый одной строкой

    klab = next(q for q in about if "Bejs-Klab" in q["question"])
    assert "2 650" in q_text(klab)
    assert "за 4 операции" in q_text(klab)
    # Кнопки категорий плюс «Своими словами» — она есть у каждого вопроса.
    codes = {option["code"] for option in klab["options"]}
    assert "sport" in codes
    assert "free" in codes
    # «Прочее» кнопкой не предлагаем: такой ответ ничего не объясняет и
    # вернул бы этот же вопрос следующим пересчётом.
    assert "other" not in codes


def q_text(question: dict) -> str:
    """Текст вопроса с обычными пробелами: в суммах стоит узкий неразрывный."""
    return re.sub(r"\s", " ", question["question"])


def test_мелочь_можно_объяснить_даже_после_откладывания(client):
    """Если процент ниже 100, вопрос нельзя потерять из очереди."""
    before = summary(client, SMALL_TG_ID)["awareness"]
    assert before["unclear"]["kopecks"] == 5_000
    assert before["percent"] < 100

    item = questions(client, tg_id=SMALL_TG_ID)[0]
    response = client.post(f"/api/v1/me/questions/{item['id']}/skip", headers=auth(SMALL_TG_ID))
    assert response.status_code == 200, response.text
    assert response.json()["open_questions"] == 1
    assert [q["id"] for q in questions(client, SMALL_TG_ID)] == [item["id"]]

    # Даже кнопка «Прочее» является ответом человека, а не слепой догадкой.
    assert answer(client, item, "free", "покупка в киоске", SMALL_TG_ID).status_code == 200
    assert questions(client, SMALL_TG_ID) == []
    after = summary(client, SMALL_TG_ID)["awareness"]
    assert after["unclear"]["kopecks"] == 0
    assert after["percent"] == 100


def test_списанные_наличные_остаются_вопросом_до_ответа(client):
    """Истечение срока меняет учёт, но не лишает человека способа объяснить сумму."""
    from tests.conftest import seed_user

    tg_id = 555_303
    seed_user(tg_id)  # фиксированное «сейчас»: 10 октября, срок уже вышел
    before = summary(client, tg_id)["awareness"]
    assert before["unallocated_cash"]["kopecks"] == 300_000
    cash = next(q for q in questions(client, tg_id) if q["kind"] == "cash_spent_on")
    assert answer(client, cash, "other", tg_id=tg_id).status_code == 200
    after = summary(client, tg_id)["awareness"]
    assert after["unallocated_cash"]["kopecks"] == 0
    assert after["unclear"]["kopecks"] == before["unclear"]["kopecks"] - 300_000


def test_остаток_сверх_возвращённого_долга_можно_объяснить(session):
    """Излишек возврата нельзя оставлять в UNKNOWN без вопроса."""
    from datetime import datetime

    from app.analytics.periods import month
    from app.analytics.reports import awareness
    from app.classify.pipeline import reclassify
    from app.db import init_db
    from app.ingest.base import ParsedRow
    from app.ingest.service import import_rows
    from app.models import OperationSource, User
    from app.service import answer_question, open_questions

    init_db()
    user = User(tg_id=555_304, display_name="Тест")
    session.add(user)
    session.flush()
    rows = [
        ParsedRow(
            booked_at=datetime(2026, 9, 1, 12), amount=-100_000,
            description="Дал в долг Ивану П.", counterparty_raw="Иван П.",
            bank="Т-Банк", last4="4127",
        ),
        ParsedRow(
            booked_at=datetime(2026, 9, 10, 12), amount=150_000,
            description="Возврат долга от Ивана П.", counterparty_raw="Иван П.",
            bank="Т-Банк", last4="4127",
        ),
    ]
    import_rows(session, user.id, rows, source="Т-Банк", origin=OperationSource.IMPORT)
    reclassify(session, user.id, now=datetime(2026, 10, 10))
    before = awareness(session, user.id, month(2026, 9))
    assert before.unknown_volume == 50_000
    residual = next(q for q in open_questions(session, user.id) if q.kind == "incoming_unknown")

    assert answer_question(session, user, residual.id, "income") is not None
    assert awareness(session, user.id, month(2026, 9)).unknown_volume == 0


def test_отрицание_перевода_себе_не_прячет_новые_вопросы(session):
    """Ответ «это разные операции» не объясняет, кому ушли деньги."""
    from datetime import datetime

    from app.classify.pipeline import reclassify
    from app.db import init_db
    from app.ingest.base import ParsedRow
    from app.ingest.service import import_rows
    from app.models import OperationSource, User
    from app.service import answer_question, open_questions

    init_db()
    user = User(tg_id=555_305, display_name="Тест")
    session.add(user)
    session.flush()
    rows = [
        ParsedRow(
            booked_at=datetime(2026, 9, 1, 12), amount=-50_000,
            description="Списание", counterparty_raw="Shop X",
            bank="Т-Банк", last4="4127",
        ),
        ParsedRow(
            booked_at=datetime(2026, 9, 1, 12, 30), amount=50_000,
            description="Перевод", counterparty_raw="Банк Z",
            bank="Альфа", last4="5555",
        ),
    ]
    import_rows(session, user.id, rows, source="банки", origin=OperationSource.IMPORT)
    reclassify(session, user.id, now=datetime(2026, 9, 25))
    transfer = next(q for q in open_questions(session, user.id) if q.kind == "is_self_transfer")

    assert answer_question(session, user, transfer.id, "no") is not None
    kinds = {q.kind for q in open_questions(session, user.id)}
    assert "is_self_transfer" not in kinds
    assert "unknown_place" in kinds  # ушедшие деньги теперь нуждаются в объяснении


def test_трата_без_названия_тоже_получает_вопрос(session):
    from datetime import datetime

    from app.analytics.periods import month
    from app.analytics.reports import awareness
    from app.classify.pipeline import reclassify
    from app.db import init_db
    from app.ingest.base import ParsedRow
    from app.ingest.service import import_rows
    from app.models import OperationSource, User
    from app.service import answer_question, open_questions

    init_db()
    user = User(tg_id=555_306, display_name="Тест")
    session.add(user)
    session.flush()
    import_rows(
        session, user.id,
        [ParsedRow(booked_at=datetime(2026, 9, 5), amount=-5_000, bank="Т-Банк")],
        source="Т-Банк", origin=OperationSource.IMPORT,
    )
    reclassify(session, user.id, now=datetime(2026, 9, 25))
    assert awareness(session, user.id, month(2026, 9)).percent < 100
    item = next(q for q in open_questions(session, user.id) if q.kind == "category_unclear")
    assert answer_question(session, user, item.id, "other") is not None
    assert open_questions(session, user.id) == []
    assert awareness(session, user.id, month(2026, 9)).percent == 100


def test_один_ответ_закрывает_все_траты_в_этом_месте(client):
    """Ответ про место — это правило для продавца, а не решение по операции."""
    klab = next(q for q in questions(client) if "Bejs-Klab" in q["question"])
    assert answer(client, klab, "sport").status_code == 200

    response = client.get(f"/api/v1/me/operations?period={PERIOD}&limit=100", headers=auth())
    named = [op for op in response.json() if "Bejs-Klab" in op["title"]]
    assert len(named) == 4
    assert all(op["category"] == "sport" for op in named)

    # И место появилось в «моих местах»: объяснено один раз и навсегда.
    mine = client.get("/api/v1/me/places", headers=auth()).json()
    assert [(p["title"], p["category"], p["operations_count"]) for p in mine] == [
        ("Bejs-Klab", "sport", 4)
    ]


def test_ответ_своими_словами_тоже_объясняет_место(client):
    """Категории в словах может и не найтись — деньги всё равно объяснены.

    «Студия печати» ни в одну категорию не читается, но смысл этих денег
    человек назвал, и держать их в непонятных было бы враньём в другую сторону.
    """
    kray = next(q for q in questions(client) if "Studio Kray" in q["question"])
    assert answer(client, kray, "free", "студия печати").status_code == 200

    response = client.get(f"/api/v1/me/operations?period={PERIOD}&limit=100", headers=auth())
    named = [op for op in response.json() if op["title"] == "Студия печати"]
    assert len(named) == 2


def test_после_ответов_непонятных_денег_не_остаётся(client):
    """Главный критерий: вопросов ноль — и осознанность ровно 100%.

    Именно это увидел живой пользователь наоборот: вопросов нет, а 19%
    денег система «не понимает».
    """
    # На случай, если что-то осталось: отвечаем на всё подряд первой кнопкой.
    for _ in range(10):
        left = questions(client)
        if not left:
            break
        for item in left:
            answer(client, item, item["options"][0]["code"])

    assert questions(client) == []
    awareness = summary(client)["awareness"]
    assert awareness["unclear"]["kopecks"] == 0
    assert awareness["percent"] == 100
