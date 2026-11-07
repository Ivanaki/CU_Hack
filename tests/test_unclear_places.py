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

# Мелочь: за месяц 50 ₽ в одном месте. Про такое спрашивать незачем.
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


def answer(client: TestClient, question: dict, code: str, text: str = ""):
    return client.post(
        f"/api/v1/me/questions/{question['id']}/answer",
        json={"code": code, "text": text},
        headers=auth(),
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


def test_мелочь_в_пятьдесят_рублей_не_спрашивают(client):
    """Порог по месту целиком: вопрос стоит внимания дороже, чем эти деньги."""
    assert questions(client, tg_id=SMALL_TG_ID) == []


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
