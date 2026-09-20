"""API: подпись Telegram, доступ к чужим данным, формы ответов."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

BOT_TOKEN = "123456:TEST-TOKEN-FOR-TESTS"
SERVICE_KEY = "test-service-key"


def make_init_data(tg_id: int, *, token: str = BOT_TOKEN, auth_date: int | None = None) -> str:
    """Собирает корректно подписанную initData, как это делает Telegram."""
    fields = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAExample",
        "user": json.dumps(
            {"id": tg_id, "first_name": "Тест", "username": "tester"}, ensure_ascii=False
        ),
    }
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


API_TG_ID = 888


@pytest.fixture(scope="module")
def client(_isolated_db):
    """Свой пользователь у API-тестов: они отвечают на вопросы и запускают
    пересчёт, а тесты сценариев проверяют исходное состояние."""
    import os

    from tests.conftest import seed_user

    seed_user(API_TG_ID)
    os.environ["BOT_TOKEN"] = BOT_TOKEN
    os.environ["INGEST_API_KEY"] = SERVICE_KEY

    from app.config import get_settings

    get_settings.cache_clear()
    from app.api.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def auth(tg_id: int = API_TG_ID) -> dict[str, str]:
    return {"X-Telegram-Init-Data": make_init_data(tg_id)}


def test_без_подписи_данные_не_отдаются(client):
    assert client.get("/api/v1/me/summary").status_code == 401


def test_подделанная_подпись_отвергается(client):
    bad = make_init_data(777, token="999:WRONG")
    response = client.get("/api/v1/me/summary", headers={"X-Telegram-Init-Data": bad})
    assert response.status_code == 401


def test_просроченная_подпись_отвергается(client):
    stale = make_init_data(777, auth_date=int(time.time()) - 10 * 24 * 3600)
    response = client.get("/api/v1/me/summary", headers={"X-Telegram-Init-Data": stale})
    assert response.status_code == 401


def test_сводка_отдаёт_всё_для_главного_экрана(client):
    data = client.get("/api/v1/me/summary?period=2026-09", headers=auth()).json()

    assert data["period"]["label"] == "Сентябрь 2026"
    assert data["spend"]["kopecks"] > 0
    assert data["spend"]["text"].endswith("₽")
    assert 0 <= data["awareness"]["percent"] <= 100
    assert data["categories"], "категории обязаны быть"
    assert data["weeks"], "недельные срезы обязаны быть"

    # Те же инварианты, что и в отчётах: экран не может показать одно, а API другое.
    assert sum(c["amount"]["kopecks"] for c in data["categories"]) == data["spend"]["kopecks"]
    assert sum(w["amount"]["kopecks"] for w in data["weeks"]) == data["spend"]["kopecks"]
    assert data["essential"]["kopecks"] + data["optional"]["kopecks"] == data["spend"]["kopecks"]


def test_чужие_данные_не_видны(client):
    """Другой telegram id — другой пользователь и пустая сводка."""
    other = client.get("/api/v1/me/summary?period=2026-09", headers=auth(999_111)).json()
    assert other["spend"]["kopecks"] == 0
    assert other["operations_count"] == 0


def test_проверка_сходимости_доступна_из_приложения(client):
    data = client.get("/api/v1/me/checkup", headers=auth()).json()
    assert data["ok"] is True
    assert data["weeks_sum_matches_month"] is True
    assert data["problems"] == []


def test_ответ_на_вопрос_меняет_осознанность(client):
    questions = client.get("/api/v1/me/questions", headers=auth()).json()
    assert questions, "в демонстрационном наборе должен остаться хотя бы один вопрос"
    target = next(q for q in questions if q["kind"] == "incoming_unknown")
    assert len(target["options"]) == 4

    before = client.get("/api/v1/me/summary?period=2026-09", headers=auth()).json()
    result = client.post(
        f"/api/v1/me/questions/{target['id']}/answer",
        json={"code": "debt_back"},
        headers=auth(),
    ).json()
    assert result["ok"] is True

    after = client.get("/api/v1/me/summary?period=2026-09", headers=auth()).json()
    assert after["awareness"]["unknown"]["kopecks"] < before["awareness"]["unknown"]["kopecks"]

    # Именно этот вопрос закрыт. Общее число открытых вопросов проверять
    # нельзя: со временем законно появляются новые — например, про наличные,
    # срок объяснения которых как раз подошёл.
    still_open = client.get("/api/v1/me/questions", headers=auth()).json()
    assert not any(q["question"] == target["question"] for q in still_open)
    assert target["id"] not in {q["id"] for q in still_open}, (
        "id вопроса не должен переиспользоваться после пересчёта"
    )

    # И главное: цифры остались согласованными после пересчёта.
    assert sum(w["amount"]["kopecks"] for w in after["weeks"]) == after["spend"]["kopecks"]


def test_mini_app_может_сам_загрузить_выписку(client):
    """Пользователь кладёт данные, не выходя из приложения."""
    from pathlib import Path

    demo = Path(__file__).resolve().parent.parent / "data" / "demo"
    blob = (demo / "cash_voice_2026-09.csv").read_bytes()

    response = client.post(
        "/api/v1/me/statements",
        files={"file": ("cash.csv", blob, "text/csv")},
        headers=auth(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    # Эти строки уже залиты сидом, поэтому ожидаем дубли, а не новые записи.
    assert body["duplicates"] >= 1
    assert body["operations"] > 0


def test_загрузка_из_mini_app_требует_подпись(client):
    """Чужой не может залить данные от имени пользователя."""
    response = client.post(
        "/api/v1/me/statements",
        files={"file": ("x.csv", b"datetime,amount\n", "text/csv")},
    )
    assert response.status_code == 401


def test_пустая_загрузка_картинок_отклоняется(client):
    response = client.post(
        "/api/v1/me/images",
        files={"files": ("empty.png", b"", "image/png")},
        headers=auth(),
    )
    assert response.status_code == 400


def test_служебная_заливка_требует_ключ(client):
    assert client.get("/api/v1/ingest/users").status_code == 401
    ok = client.get("/api/v1/ingest/users", headers={"X-API-Key": SERVICE_KEY})
    assert ok.status_code == 200


def test_заливка_траты_за_пользователя_заводит_его_сам(client):
    response = client.post(
        "/api/v1/ingest/expense",
        json={"tg_id": 424_242, "text": "кофе 300"},
        headers={"X-API-Key": SERVICE_KEY},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["added"] == 1

    users = client.get("/api/v1/ingest/users", headers={"X-API-Key": SERVICE_KEY}).json()
    assert any(u["tg_id"] == 424_242 for u in users)
