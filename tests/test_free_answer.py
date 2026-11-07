"""Ответ своими словами: когда ни один готовый вариант не подходит.

«Перевод Дмитрий К. на 25 000 ₽» бывает и арендой, и возвратом за ремонт —
кнопками это не перечислить. Проверяем, что написанный человеком текст
закрывает вопрос, попадает в категорию, если она из слов читается, и в
любом случае убирает деньги из неразобранных: человек объяснил их.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import NOW
from tests.test_api import BOT_TOKEN, make_init_data

FREE_TG_ID = 555_101

# Пять переводов физлицам без назначения: каждый даёт свой вопрос
# «что это?», и тесты не отбирают вопросы друг у друга.
STATEMENT = """Дата операции;Номер счета;Сумма;Валюта;Назначение платежа
11.09.2026;40817810900000005531;-25000,00;RUB;Перевод Дмитрий К.
12.09.2026;40817810900000005531;-3400,00;RUB;Перевод Ольга С.
13.09.2026;40817810900000005531;-7000,00;RUB;Перевод Павел Р.
14.09.2026;40817810900000005531;-12000,00;RUB;Перевод Егор Т.
15.09.2026;40817810900000005531;-5000,00;RUB;Перевод Илья В.
"""


def auth(tg_id: int = FREE_TG_ID) -> dict[str, str]:
    return {"X-Telegram-Init-Data": make_init_data(tg_id, token=BOT_TOKEN)}


@pytest.fixture(scope="module")
def client(_isolated_db):
    """Свой пользователь и своя выписка: тесты отвечают на вопросы и
    запускают пересчёт, а соседние файлы проверяют исходное состояние."""
    import os

    from tests.conftest import seed_user

    seed_user(FREE_TG_ID)
    os.environ["BOT_TOKEN"] = BOT_TOKEN

    from app.config import get_settings

    get_settings.cache_clear()
    from app.api.app import create_app

    with TestClient(create_app()) as test_client:
        response = test_client.post(
            "/api/v1/me/statements",
            files={"file": ("alfa_free.csv", STATEMENT.encode("utf-8"), "text/csv")},
            headers=auth(),
        )
        assert response.status_code == 200, response.text
        yield test_client
    get_settings.cache_clear()


def questions(client: TestClient) -> list[dict]:
    return client.get("/api/v1/me/questions?limit=100", headers=auth()).json()


def question_about(client: TestClient, who: str) -> dict:
    return next(q for q in questions(client) if who in q["question"])


def answer(client: TestClient, question: dict, text: str):
    return client.post(
        f"/api/v1/me/questions/{question['id']}/answer",
        json={"code": "free", "text": text},
        headers=auth(),
    )


def summary(client: TestClient) -> dict:
    return client.get("/api/v1/me/summary?period=2026-09", headers=auth()).json()


def operations(client: TestClient) -> list[dict]:
    return client.get(
        "/api/v1/me/operations?period=2026-09&limit=500", headers=auth()
    ).json()


def test_у_каждого_вопроса_есть_кнопка_своими_словами(client):
    """Контракт с Mini App: вариант «free» последний и есть везде, иначе
    человеку некуда деться, когда ни один готовый ответ не подходит."""
    assert questions(client), "в наборе обязаны быть открытые вопросы"
    for question in questions(client):
        assert question["options"][-1]["code"] == "free"
        assert question["options"][-1]["label"] == "Своими словами"


def test_свободный_ответ_закрывает_вопрос_и_уменьшает_неразобранное(client):
    """Смысл кнопки: человек объяснил деньги — значит, они больше не висят
    на «не знаю, что это», и спрашивать об этом второй раз не за что."""
    target = question_about(client, "Дмитрий К.")
    before = summary(client)

    response = answer(client, target, "вернул Диме за ремонт")
    assert response.status_code == 200, response.text

    after = summary(client)
    assert after["awareness"]["unknown"]["kopecks"] == (
        before["awareness"]["unknown"]["kopecks"] - 2_500_000
    ), "25 000 ₽ перестали быть операцией без установленного смысла"
    assert not any("Дмитрий К." in q["question"] for q in questions(client))


def test_текст_с_понятной_категорией_попадает_именно_в_эту_категорию(client):
    """«Такси до аэропорта» обязано лечь в «Такси» — ровно так же, как если
    бы человек ткнул в кнопку категории. Иначе текст был бы декорацией."""
    target = question_about(client, "Ольга С.")
    assert answer(client, target, "такси до аэропорта").status_code == 200

    op = next(o for o in operations(client) if o["amount"]["kopecks"] == 340_000)
    assert op["category"] == "taxi"
    assert "такси до аэропорта" in op["title"].lower(), "слова человека — в названии"
    assert "такси до аэропорта" in op["explanation"].lower()


def test_текст_без_категории_всё_равно_убирает_операцию_из_непонятных(client):
    """Словарь категорий не знает про ремонт балкона, и это нормально:
    человек назвал смысл денег, значит, они больше не «неясные», а
    осознанность обязана вырасти, а не остаться прежней."""
    target = question_about(client, "Егор Т.")
    before = summary(client)

    assert answer(client, target, "ремонт балкона у Егора").status_code == 200

    after = summary(client)
    assert after["awareness"]["unclear"]["kopecks"] == (
        before["awareness"]["unclear"]["kopecks"] - 1_200_000
    )
    assert after["awareness"]["percent"] > before["awareness"]["percent"]

    op = next(o for o in operations(client) if o["amount"]["kopecks"] == 1_200_000)
    assert op["counts_as_spending"] is True, "деньги ушли — трату не отменяем"
    assert "ремонт балкона" in op["title"].lower()


def test_пустой_текст_своими_словами_отвергается_а_вопрос_остаётся(client):
    """Пустой ответ ничего не объясняет. Молча закрыть вопрос — потерять
    деньги из виду, поэтому это отказ с причиной, а не «ок»."""
    target = question_about(client, "Павел Р.")

    response = answer(client, target, "   ")
    assert response.status_code == 400
    assert "пуст" in response.json()["detail"].lower(), "отказ обязан объяснить причину"

    assert any(q["id"] == target["id"] for q in questions(client)), "вопрос остался открытым"


def test_свободный_ответ_переживает_полный_пересчёт(client, session):
    """Решения живут отдельно от операций, и пересчёт пересобирает всё с
    нуля. Если ответ словами его не переживёт, вопрос задастся заново, а
    название операции вернётся к безликому «Перевод»."""
    from app.classify.pipeline import reclassify
    from app.models import User

    target = question_about(client, "Илья В.")
    assert answer(client, target, "вернул Илье за бензин").status_code == 200

    user_id = session.scalar(select(User.id).where(User.tg_id == FREE_TG_ID))
    reclassify(session, user_id, now=NOW)
    session.commit()

    assert not any("Илья В." in q["question"] for q in questions(client))
    op = next(o for o in operations(client) if o["amount"]["kopecks"] == 500_000)
    assert "вернул илье за бензин" in op["title"].lower()


def test_чужой_вопрос_нельзя_закрыть_своими_словами(client):
    """Текст — не обход проверки владельца: чужой вопрос по-прежнему чужой."""
    target = question_about(client, "Катя Н.")

    response = client.post(
        f"/api/v1/me/questions/{target['id']}/answer",
        json={"code": "free", "text": "это возврат за обед"},
        headers=auth(999_112),
    )
    assert response.status_code == 404
    assert any(q["id"] == target["id"] for q in questions(client)), "вопрос остался открытым"
