"""API: подпись Telegram, доступ к чужим данным, формы ответов."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import quote, urlencode

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


def test_кириллица_в_подписи_это_отказ_а_не_пятисотка(client):
    """`hash` приходит из запроса, и не-ASCII в нём ломал сравнение строк.

    Заголовок остаётся ASCII — кириллица проезжает процентным
    кодированием, — а после разбора initData сравнивать приходится уже её.
    """
    response = client.get(
        "/api/v1/me/summary",
        headers={"X-Telegram-Init-Data": f"user=%7B%22id%22%3A777%7D&hash={quote('подпись')}"},
    )
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
    assert target["person_label"] == "Катя Н."
    # Четыре готовых варианта и «своими словами» последним: тап закрывает
    # вопрос, а если ни один вариант не подходит — закрывает текст.
    assert [o["code"] for o in target["options"]][-1] == "free"
    assert len(target["options"]) == 5

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


def test_игровое_состояние_доступно_в_собранном_приложении(client):
    """Роутер игры зарегистрирован ДО StaticFiles на корне.

    Проверка не про расчёт — он покрыт в tests/test_gamification.py, где
    роутер поднят отдельным приложением. Здесь важно ровно одно: маршрут
    достижим в том приложении, которое поднимается на демонстрации. Если
    include_router(game) уедет ниже app.mount("/", StaticFiles(...)),
    статика перехватит адрес и ответит 405 — молча, без единого падения
    в остальных тестах.
    """
    assert client.get("/api/v1/me/game").status_code == 401, (
        "адрес перехвачен статикой: без подписи должен отвечать сам роутер"
    )

    response = client.get("/api/v1/me/game?period=2026-09", headers=auth())
    assert response.status_code == 200, response.text
    data = response.json()

    # Форма — дословно по утверждённому контракту: по нему написан фронт.
    assert set(data) == {"level", "quests", "achievements", "map"}
    assert set(data["level"]) == {"number", "title", "xp", "xp_total_next", "progress"}
    assert 0.0 <= data["level"]["progress"] <= 1.0
    for quest in data["quests"]:
        assert set(quest) == {
            "code", "kind", "title", "detail", "progress",
            "xp", "done", "sponsor", "reward", "deadline",
        }
        assert quest["kind"] in {"system", "sponsor"}
    for achievement in data["achievements"]:
        assert set(achievement) == {
            "code", "title", "detail", "earned", "earned_at", "progress", "hint",
        }
    for station in data["map"]:
        assert set(station) == {"code", "kind", "state", "title", "detail", "amount", "action"}
        assert station["kind"] in {"week", "question", "chest", "quest", "finish"}
        assert station["state"] in {"done", "current", "locked"}


def test_карта_и_сводка_открываются_на_одном_месяце(client):
    """Иначе сумма станций на карте не сойдётся с суммой на главном экране."""
    game = client.get("/api/v1/me/game?period=2026-09", headers=auth()).json()
    summary = client.get("/api/v1/me/summary?period=2026-09", headers=auth()).json()

    weeks = [st for st in game["map"] if st["kind"] == "week"]
    assert sum(st["amount"]["kopecks"] for st in weeks) == summary["spend"]["kopecks"]


def test_гонка_при_первом_открытии_не_роняет_запрос(_isolated_db, monkeypatch):
    """Mini App при открытии дёргает четыре ручки параллельно.

    У нового человека все четыре не находят его в базе и все четыре
    пытаются завести. Одна выигрывает, остальные упираются в уникальный
    индекс — и человек на первом же экране видит «API недоступен». Ровно
    это увидел живой пользователь на боевом адресе.

    Гонка воспроизводится честно, но без потоков: файловая база в тестах
    одна на процесс, и параллельные соединения ловили бы «database is
    locked» вместо проверяемого случая. Поэтому первому SELECT велено
    вернуть пустоту — то же самое, что видит проигравший в гонке, пока
    сосед ещё не закоммитил.
    """
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from app.db import init_db, session_scope
    from app.models import User
    from app.service import get_or_create_user

    init_db()
    tg_id = 7_654_321
    with session_scope() as session:
        get_or_create_user(session, tg_id, "Сосед")

    seen = {"selects": 0}
    real_scalar = Session.scalar

    def blind_first(self, *args, **kwargs):
        seen["selects"] += 1
        if seen["selects"] == 1:
            return None
        return real_scalar(self, *args, **kwargs)

    monkeypatch.setattr(Session, "scalar", blind_first)
    with session_scope() as session:
        user = get_or_create_user(session, tg_id, "Проигравший гонку")
        assert user.tg_id == tg_id

    monkeypatch.undo()
    with session_scope() as session:
        count = session.scalar(
            select(func.count()).select_from(User).where(User.tg_id == tg_id)
        )
    assert count == 1, f"человек заведён {count} раз вместо одного"
