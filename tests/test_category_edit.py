"""Ручная смена категории одной траты и всех трат места."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.test_api import BOT_TOKEN, make_init_data

USER_ID = 555_310
PERIOD = "2026-09"
STATEMENT = """Дата операции;Номер счета;Сумма;Валюта;Назначение платежа
05.09.2026;40817810900000005531;-450,00;RUB;InterFood
08.09.2026;40817810900000005531;-780,00;RUB;INTERFOOD 12
12.09.2026;40817810900000005531;-620,00;RUB;Интерфуд
14.09.2026;40817810900000005531;-1340,00;RUB;Kvartal Market
"""


def auth(user_id: int = USER_ID) -> dict[str, str]:
    return {"X-Telegram-Init-Data": make_init_data(user_id, token=BOT_TOKEN)}


@pytest.fixture
def client(_isolated_db, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", BOT_TOKEN)
    from app.api.app import create_app
    from app.config import get_settings

    get_settings.cache_clear()
    with TestClient(create_app()) as app:
        result = app.post(
            "/api/v1/me/statements",
            files={"file": ("categories.csv", STATEMENT.encode(), "text/csv")},
            headers=auth(),
        )
        assert result.status_code == 200, result.text
        yield app
    get_settings.cache_clear()


def operations(client: TestClient) -> list[dict]:
    result = client.get(f"/api/v1/me/operations?period={PERIOD}", headers=auth())
    assert result.status_code == 200, result.text
    return result.json()


def change(client: TestClient, operation_id: int, category: str, scope: str):
    return client.post(
        f"/api/v1/me/operations/{operation_id}/category",
        json={"category": category, "scope": scope},
        headers=auth(),
    )


def by_amount(rows: list[dict], amount: int) -> dict:
    return next(op for op in rows if op["amount"]["kopecks"] == amount)


def test_one_expense_survives_reclassification(client):
    before = operations(client)
    target = by_amount(before, 45_000)
    other = by_amount(before, 78_000)
    response = change(client, target["id"], "cafe", "operation")
    assert response.status_code == 200, response.text
    after = operations(client)
    assert by_amount(after, 45_000)["category"] == "cafe"
    assert by_amount(after, 78_000)["category"] == other["category"]

    from app.classify.pipeline import reclassify
    from app.db import session_scope
    from app.models import User

    with session_scope() as session:
        user = session.query(User).filter_by(tg_id=USER_ID).one()
        reclassify(session, user.id)
    assert by_amount(operations(client), 45_000)["category"] == "cafe"


def test_place_scope_and_rename_preserve_category(client):
    target = next(op for op in operations(client) if op["amount"]["kopecks"] == 45_000)
    assert change(client, target["id"], "electronics", "operation").status_code == 200
    target = by_amount(operations(client), 45_000)
    response = change(client, target["id"], "cafe", "place")
    assert response.status_code == 200, response.text
    assert response.json()["applied"] == 3
    assert sum(op["category"] == "cafe" for op in operations(client)) == 3
    assert by_amount(operations(client), 45_000)["category"] == "cafe"

    renamed = client.post(
        "/api/v1/me/places",
        json={"merchant": "InterFood", "title": "Узбечка", "category": "cafe"},
        headers=auth(),
    )
    assert renamed.status_code == 200, renamed.text
    assert sum(
        op["title"] == "Узбечка" and op["category"] == "cafe"
        for op in operations(client)
    ) == 3

    later = """Дата операции;Номер счета;Сумма;Валюта;Назначение платежа
25.09.2026;40817810900000005531;-900,00;RUB;InterFood
"""
    imported = client.post(
        "/api/v1/me/statements",
        files={"file": ("later.csv", later.encode(), "text/csv")},
        headers=auth(),
    )
    assert imported.status_code == 200, imported.text
    assert sum(
        op["title"] == "Узбечка" and op["category"] == "cafe"
        for op in operations(client)
    ) == 4


def test_invalid_category_and_other_user(client):
    target = operations(client)[0]
    assert change(client, target["id"], "salary", "operation").status_code == 400
    assert change(client, target["id"], "cafe", "unknown").status_code == 400
    foreign = client.post(
        f"/api/v1/me/operations/{target['id']}/category",
        json={"category": "cafe", "scope": "operation"},
        headers=auth(USER_ID + 1),
    )
    assert foreign.status_code == 404
    choices = client.get("/api/v1/me/categories", headers=auth())
    assert choices.status_code == 200
    assert {item["code"] for item in choices.json()} >= {"cafe", "groceries"}
