"""Виджет на рабочем столе iPhone: тап, карточка, ответ одним тапом.

Проверяется то, что ломается молча и обнаруживается только на защите:
чужой не читает чужую карточку, служебный ключ не утекает в браузер,
пустой тап не накручивает метрику «действий в день», а ответ с карточки
поднимает процент осознанности так же, как ответ в боте.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVICE_KEY = "test-widget-key"
QUICK_TG_ID = 889


@pytest.fixture(scope="module")
def client(_isolated_db):
    """Свой пользователь: тесты виджета отвечают на вопросы и пересчитывают всё."""
    import os

    from tests.conftest import seed_user

    seed_user(QUICK_TG_ID)
    os.environ["INGEST_API_KEY"] = SERVICE_KEY

    from app.config import get_settings

    get_settings.cache_clear()
    from app.api.quick import quick

    # Отдельное приложение из одного роутера: подключение в app.py — забота
    # сборки, а тест обязан проверять сам маршрут, а не порядок include.
    app = FastAPI()
    app.include_router(quick)
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


KEY = {"X-API-Key": SERVICE_KEY}


def tap(client, text: str = "", headers: dict | None = None):
    return client.post(
        "/api/v1/quick/tap",
        json={"tg_id": QUICK_TG_ID, "text": text},
        headers={**KEY, **(headers or {})},
    )


def test_без_ключа_виджет_ничего_не_делает(client):
    response = client.post("/api/v1/quick/tap", json={"tg_id": QUICK_TG_ID, "text": "кофе 300"})
    assert response.status_code == 401


def test_чужой_ключ_не_подходит(client):
    response = client.post(
        "/api/v1/quick/tap",
        json={"tg_id": QUICK_TG_ID, "text": "кофе 300"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert response.status_code == 401


def test_карточку_без_токена_и_без_ключа_не_отдают(client):
    assert client.get("/api/v1/quick/card").status_code == 401
    assert client.get(f"/api/v1/quick/card?tg_id={QUICK_TG_ID}").status_code == 401


def test_тап_с_диктовкой_записывает_наличные_и_возвращает_карточку(client):
    # Первый ввод двигает точку отсчёта пересчёта на «сейчас», и часть
    # отложенных решений (наличные, доли по общему счёту) пересматривается.
    # Мерить прирост надо после этого сдвига, иначе тест ловит не свою цифру.
    tap(client, "парковка 100")
    before = tap(client).json()["spent_kopecks"]
    card = tap(client, "кофе 300").json()

    assert card["ok"]
    assert "300" in card["headline"], card["headline"]
    # Наличная трата не добавляет денег в месяц второй раз: снятые из
    # банкомата уже посчитаны. Пока остаток снятия не списан по сроку,
    # трата забирает часть остатка, и месяц растёт на неё; после срока
    # остаток уже лежит в расходах, и месяц не меняется. Верны обе ветки,
    # и какая из них сработает — зависит от сегодняшней даты, поэтому
    # проверяется правило целиком, а не одна ветка. Раньше здесь стояло
    # «ровно +300», и тест ломался сам по себе через две недели после
    # снятия в демонстрационном наборе.
    growth = card["spent_kopecks"] - before
    assert growth in (0, 30000), f"наличные посчитаны неверно: прирост {growth}"
    assert card["awareness"] <= 100
    assert card["notification"], "для «Показать уведомление» нужна готовая строка"


def test_нераспознанная_диктовка_не_роняет_виджет(client):
    card = tap(client, "э-э-э").json()
    assert card["ok"], "виджет обязан показать карточку даже когда не понял сказанное"
    assert card["error"]
    assert card["headline"].startswith("Не разобрал")


def test_пустой_тап_не_считается_действием(client):
    """Метрика считает то, чего инструмент требует, а не то, что человек смотрит."""
    before = tap(client).json()["actions_per_day"]
    assert tap(client).json()["actions_per_day"] == before
    assert tap(client, "ничего").json()["actions_per_day"] == before


def test_ссылка_на_карточку_ведёт_на_страницу_и_несёт_токен(client):
    card = tap(client).json()
    assert "/w.html?t=" in card["card_url"]
    assert card["token"] in card["card_url"]


def test_ссылка_собирается_по_заголовкам_прокси(client):
    """За туннелем uvicorn видит http — Safari показал бы предупреждение."""
    card = tap(
        client,
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "look-own-alt-electronics.trycloudflare.com",
        },
    ).json()
    assert card["card_url"].startswith("https://look-own-alt-electronics.trycloudflare.com/w.html")


def test_по_токену_карточка_читается_а_ключ_в_браузер_не_уезжает(client):
    token = tap(client).json()["token"]
    response = client.get(f"/api/v1/quick/card?t={token}")
    assert response.status_code == 200
    assert response.json()["spent_kopecks"] > 0


def test_подделанный_и_просроченный_токен_отвергаются(client):
    from app.api.quick import make_token

    good = make_token(QUICK_TG_ID, SERVICE_KEY)
    head, exp, sig = good.split(".")

    assert client.get(f"/api/v1/quick/card?t={head}.{exp}.{'0' * len(sig)}").status_code == 401
    assert client.get("/api/v1/quick/card?t=мусор").status_code == 401

    stale = make_token(QUICK_TG_ID, SERVICE_KEY, ttl=-1)
    assert client.get(f"/api/v1/quick/card?t={stale}").status_code == 401


def test_токен_привязан_к_своему_пользователю(client):
    """Подставить чужой tg_id рядом с токеном нельзя: токен важнее параметра."""
    from app.api.quick import make_token

    token = make_token(QUICK_TG_ID, SERVICE_KEY)
    mine = client.get(f"/api/v1/quick/card?t={token}").json()
    spoofed = client.get(f"/api/v1/quick/card?t={token}&tg_id=424242").json()
    assert spoofed["spent_kopecks"] == mine["spent_kopecks"]


def test_токен_подписан_ключом_а_не_просто_склеен(client):
    """Ключ сменили — все выданные пропуска перестают работать."""
    from app.api.quick import make_token

    foreign = make_token(QUICK_TG_ID, "другой-ключ")
    assert client.get(f"/api/v1/quick/card?t={foreign}").status_code == 401


def test_карточка_показывает_ровно_один_вопрос_с_кнопками(client):
    card = client.get(f"/api/v1/quick/card?tg_id={QUICK_TG_ID}", headers=KEY).json()
    assert card["open_questions"] >= 1, "в демонстрационном наборе есть спорные операции"
    assert card["question"], "карточка обязана показать, что именно спрашивают"
    assert card["question"]["options"], "вопрос без кнопок закрыть одним тапом нельзя"


def test_ответ_с_карточки_поднимает_осознанность_и_убирает_вопрос(client):
    before = client.get(f"/api/v1/quick/card?tg_id={QUICK_TG_ID}", headers=KEY).json()
    question = before["question"]

    after = client.post(
        f"/api/v1/quick/answer?tg_id={QUICK_TG_ID}",
        json={"review_id": question["id"], "code": question["options"][0]["code"]},
        headers=KEY,
    ).json()

    assert after["open_questions"] == before["open_questions"] - 1
    assert after["awareness"] >= before["awareness"], "ответ не может ухудшить осознанность"
    assert after["headline"] == "Ответ принят"


def test_на_закрытый_вопрос_второй_раз_не_ответить(client):
    response = client.post(
        f"/api/v1/quick/answer?tg_id={QUICK_TG_ID}",
        json={"review_id": 999999, "code": "что_угодно"},
        headers=KEY,
    )
    assert response.status_code == 404


def test_карточка_отдаёт_готовые_строки_а_не_копейки(client):
    """Shortcuts не умеет форматировать деньги: каждое «собери строку» — лишний шаг."""
    card = client.get(f"/api/v1/quick/card?tg_id={QUICK_TG_ID}", headers=KEY).json()
    assert card["spent"].endswith("₽")
    assert card["unclear"].endswith("₽")
    assert isinstance(card["awareness"], int)
    assert card["period"], "человек должен видеть, за какой месяц цифра"


def test_токен_переживает_перезапуск_сервиса():
    """Без состояния на сервере: перезапуск посреди показа не гасит карточку."""
    from app.api.quick import make_token, read_token

    token = make_token(QUICK_TG_ID, SERVICE_KEY, ttl=5)
    assert read_token(token, SERVICE_KEY) == QUICK_TG_ID
    assert int(token.split(".")[1]) >= int(time.time())
