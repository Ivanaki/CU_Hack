"""Мои места: человек один раз называет магазин по-своему.

Живой случай, с которого это пришло: голосом сказано «350 на узбечку», а в
выписке тот же ларёк проходит как «InterFood». Человеку нужно одно название
на оба следа — и на то, что уже загружено, и на то, что приедет потом.

Проверяется то, что ломается молча: правило, которое не доехало до прошлого;
три написания одного продавца, ставшие тремя местами; название, пережившее
пересчёт только до первого пересчёта; и деньги, поехавшие вслед за именем.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_api import BOT_TOKEN, make_init_data

PLACES_TG_ID = 555_202
STRANGER_TG_ID = 555_203

# Три написания одного ларька и один посторонний магазин, который обязан
# остаться при своём имени: правило не должно расползаться на соседей.
STATEMENT = """Дата операции;Номер счета;Сумма;Валюта;Назначение платежа
05.09.2026;40817810900000005531;-450,00;RUB;InterFood
08.09.2026;40817810900000005531;-780,00;RUB;INTERFOOD 12
12.09.2026;40817810900000005531;-620,00;RUB;Интерфуд
14.09.2026;40817810900000005531;-1340,00;RUB;Kvartal Market
"""

# Приезжает уже после того, как место названо.
LATER = """Дата операции;Номер счета;Сумма;Валюта;Назначение платежа
25.09.2026;40817810900000005531;-900,00;RUB;InterFood
"""

PERIOD = "2026-09"


def auth(tg_id: int = PLACES_TG_ID) -> dict[str, str]:
    return {"X-Telegram-Init-Data": make_init_data(tg_id, token=BOT_TOKEN)}


@pytest.fixture(scope="module")
def client(_isolated_db):
    """Свой пользователь и своя выписка: тесты переименовывают продавцов и
    пересчитывают книгу, а соседние файлы проверяют исходное состояние."""
    import os

    os.environ["BOT_TOKEN"] = BOT_TOKEN

    from app.config import get_settings

    get_settings.cache_clear()
    from app.api.app import create_app

    with TestClient(create_app()) as test_client:
        response = test_client.post(
            "/api/v1/me/statements",
            files={"file": ("alfa_places.csv", STATEMENT.encode("utf-8"), "text/csv")},
            headers=auth(),
        )
        assert response.status_code == 200, response.text
        yield test_client
    get_settings.cache_clear()


def operations(client: TestClient, tg_id: int = PLACES_TG_ID) -> list[dict]:
    response = client.get(f"/api/v1/me/operations?period={PERIOD}&limit=200", headers=auth(tg_id))
    assert response.status_code == 200, response.text
    return response.json()


def titles(client: TestClient) -> list[str]:
    return [op["title"] for op in operations(client)]


def places(client: TestClient, tg_id: int = PLACES_TG_ID) -> list[dict]:
    response = client.get("/api/v1/me/places", headers=auth(tg_id))
    assert response.status_code == 200, response.text
    return response.json()


def name_place(
    client: TestClient,
    merchant: str,
    title: str,
    category: str | None = None,
    *,
    tg_id: int = PLACES_TG_ID,
):
    return client.post(
        "/api/v1/me/places",
        json={"merchant": merchant, "title": title, "category": category},
        headers=auth(tg_id),
    )


def spend_kopecks(client: TestClient) -> int:
    response = client.get(f"/api/v1/me/summary?period={PERIOD}", headers=auth())
    assert response.status_code == 200, response.text
    return response.json()["spend"]["kopecks"]


def test_название_встаёт_на_уже_загруженные_операции(client):
    """Главное обещание: назвал один раз — и прошлое тоже переименовалось."""
    was_spent = spend_kopecks(client)

    response = name_place(client, "InterFood", "Узбечка", "cafe")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] == 3

    assert titles(client).count("Узбечка") == 3
    # Правило меняет название и категорию, но не деньги.
    assert spend_kopecks(client) == was_spent


def test_разные_написания_продавца_это_одно_место(client):
    """«InterFood», «INTERFOOD 12» и «Интерфуд» — один ларёк, а не три.

    Ни точное совпадение ключа, ни общий алфавит тут не помогают: ради
    этого в places.py и живёт транслитерация с допуском на написание.
    """
    named = [op for op in operations(client) if op["title"] == "Узбечка"]
    assert len(named) == 3
    assert {op["amount"]["kopecks"] for op in named} == {45_000, 78_000, 62_000}
    assert all(op["category"] == "cafe" for op in named)
    # Посторонний магазин под правило не попал.
    assert "Kvartal Market" in titles(client)


def test_правило_объясняет_операцию_словами_человека(client):
    named = next(op for op in operations(client) if op["title"] == "Узбечка")
    assert "Узбечка" in named["explanation"]
    assert named["place"] == "interfood"


def test_название_применяется_к_будущей_операции(client):
    """Правило живёт в своей таблице, поэтому работает и на том, что приедет."""
    response = client.post(
        "/api/v1/me/statements",
        files={"file": ("alfa_later.csv", LATER.encode("utf-8"), "text/csv")},
        headers=auth(),
    )
    assert response.status_code == 200, response.text
    assert titles(client).count("Узбечка") == 4
    assert places(client)[0]["operations_count"] == 4


def test_название_переживает_полный_пересчёт(client):
    """Пересчёт сносит все операции и собирает их заново из сырых строк.

    Если бы имя хранилось в операции, здесь оно и потерялось бы.
    """
    from app.classify.pipeline import reclassify
    from app.db import session_scope
    from app.models import User

    with session_scope() as session:
        user = session.scalar(select(User).where(User.tg_id == PLACES_TG_ID))
        reclassify(session, user.id)

    assert titles(client).count("Узбечка") == 4


def test_голосовая_запись_попадает_в_то_же_место(client):
    """«350 на узбечку» — то же место, что и операции с карты.

    Падеж и предлог помехой не будут: сопоставление то же самое, что у
    выписки. А вот связать «узбечку» с «InterFood» самим было бы выдумкой —
    их свело явное действие человека выше по файлу.
    """
    response = client.post(
        "/api/v1/me/expenses",
        json={"text": "350 на узбечку", "occurred_at": "2026-09-27T13:00:00"},
        headers=auth(),
    )
    assert response.status_code == 200, response.text

    voiced = [
        op
        for op in operations(client)
        if op["title"] == "Узбечка" and op["amount"]["kopecks"] == 35_000
    ]
    assert len(voiced) == 1
    assert voiced[0]["category"] == "cafe"
    assert titles(client).count("Узбечка") == 5


def test_список_мест_показывает_название_и_категорию(client):
    mine = places(client)
    assert len(mine) == 1
    assert mine[0]["pattern"] == "interfood"
    assert mine[0]["title"] == "Узбечка"
    assert mine[0]["category"] == "cafe"
    assert mine[0]["category_title"] == "Кафе и рестораны"
    assert mine[0]["operations_count"] == 5


def test_книга_сходится_после_переименования(client):
    """Имя — это имя. Проводки по операции обязаны по-прежнему давать ноль."""
    response = client.get(f"/api/v1/me/checkup?period={PERIOD}", headers=auth())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True, body["problems"]
    assert body["weeks_sum_matches_month"] is True


def test_пустое_название_не_принимается(client):
    """400 с внятной причиной, а не 422 с разбором схемы."""
    response = name_place(client, "Kvartal Market", "   ", "groceries")
    assert response.status_code == 400
    assert "назван" in response.json()["detail"]
    # Ничего не создалось и ничего не переименовалось.
    assert len(places(client)) == 1
    assert "Kvartal Market" in titles(client)


def test_несуществующая_категория_не_принимается(client):
    response = name_place(client, "Kvartal Market", "Квартал", "нет_такой")
    assert response.status_code == 400
    assert len(places(client)) == 1


def test_чужое_место_не_видно_и_не_удаляется(client):
    """Правила лежат по владельцу, и чужое для соседа просто не существует."""
    assert places(client, tg_id=STRANGER_TG_ID) == []

    response = client.delete("/api/v1/me/places/interfood", headers=auth(STRANGER_TG_ID))
    assert response.status_code == 404

    # У владельца всё на месте.
    assert titles(client).count("Узбечка") == 5


def test_удаление_возвращает_банковское_название(client):
    """Убрали правило — вернулось то имя, которое даёт банк."""
    response = client.delete("/api/v1/me/places/interfood", headers=auth())
    assert response.status_code == 200, response.text

    back = titles(client)
    assert "Узбечка" not in back
    assert "InterFood" in back
    assert "Интерфуд" in back
    assert places(client) == []

    # И повторное удаление честно говорит, что удалять нечего.
    assert client.delete("/api/v1/me/places/interfood", headers=auth()).status_code == 404
