"""Кто прислал файл и что из него получилось.

Адрес у /upload один на всех телефонах, поэтому опознание — единственное,
что отделяет мою книгу от чужой. Раньше опознавал tg_id из запроса: он не
секрет, его видит каждый, кому человек писал, и его можно перебрать, то
есть посторонний мог залить операции в чужую аналитику. Здесь проверяется,
что этого больше нельзя, а заодно — что по файлу видно его операции, а по
операции виден файл.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from tests.test_uploads import m4a, png

BASE_URL = "https://example.test"
SERVICE_KEY = "identity-service-key"

TG_OWNER = 991_001
TG_STRANGER = 991_002
TG_VICTIM = 991_003  # за него пытаются заливать по tg_id
TG_SERVICE = 991_004  # за него заливаем служебным ключом, это законно


# --- обвязка ---------------------------------------------------------------


@pytest.fixture(scope="module")
def upload_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("identity-uploads")


@pytest.fixture(scope="module")
def client(_isolated_db, upload_dir: Path):
    """Свой каталог загрузок и известный служебный ключ.

    BOT_TOKEN пуст намеренно: без него Mini App пускает по X-Debug-Tg-Id, и
    ручки про ссылку можно проверять, не подписывая initData.
    """
    saved = {
        name: os.environ.get(name)
        for name in ("UPLOAD_DIR", "PUBLIC_BASE_URL", "UPLOAD_API_KEY",
                     "INGEST_API_KEY", "BOT_TOKEN", "MAX_UPLOAD_MB")
    }
    os.environ["UPLOAD_DIR"] = str(upload_dir)
    os.environ["PUBLIC_BASE_URL"] = BASE_URL
    os.environ["UPLOAD_API_KEY"] = ""
    os.environ["INGEST_API_KEY"] = SERVICE_KEY
    os.environ["BOT_TOKEN"] = ""
    os.environ["MAX_UPLOAD_MB"] = "20"

    from app.config import get_settings

    get_settings.cache_clear()
    from app.api.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client

    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    get_settings.cache_clear()


def issue(tg_id: int) -> str:
    """Выдаёт человеку его ссылку так же, как это делает бот."""
    from app.db import session_scope
    from app.service import get_or_create_user, issue_upload_token

    with session_scope() as session:
        return issue_upload_token(session, get_or_create_user(session, tg_id, "Тест"))


def rotate(tg_id: int) -> str:
    from app.db import session_scope
    from app.service import get_or_create_user, rotate_upload_token

    with session_scope() as session:
        return rotate_upload_token(session, get_or_create_user(session, tg_id))


def operations_of(tg_id: int) -> int:
    """Сколько операций в книге человека. Ноль — если человека вообще нет."""
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Operation, User

    with session_scope() as session:
        user = session.scalar(select(User).where(User.tg_id == tg_id))
        if user is None:
            return 0
        return int(
            session.scalar(
                select(func.count()).select_from(Operation).where(Operation.user_id == user.id)
            )
            or 0
        )


def user_exists(tg_id: int) -> bool:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import User

    with session_scope() as session:
        return session.scalar(select(User).where(User.tg_id == tg_id)) is not None


def vision(monkeypatch, rows: int = 2, *, merchant: str = "Кофейня") -> None:
    """Подменяет модель зрения: тесты про опознание, а не про распознавание."""
    from app.ingest import vision as vision_mod
    from app.ingest.base import ParsedRow

    def parse_images(images, **kwargs):
        return vision_mod.VisionResult(
            "statement",
            [
                ParsedRow(
                    booked_at=datetime(2026, 9, 12, 10, index),
                    amount=-45000 - index,
                    description=f"{merchant} {index}",
                    bank="Т-Банк",
                )
                for index in range(rows)
            ],
            "Т-Банк",
        )

    monkeypatch.setattr(vision_mod, "parse_images", parse_images)


def upload(client, data: bytes = None, *, content_type: str = "image/png", **headers):
    return client.post(
        "/upload",
        content=png() if data is None else data,
        headers={"Content-Type": content_type, **headers},
    )


def debug_auth(tg_id: int) -> dict[str, str]:
    return {"X-Debug-Tg-Id": str(tg_id)}


# --- опознание по личной ссылке --------------------------------------------


def test_ссылка_опознаёт_своего_хозяина(client, monkeypatch):
    vision(monkeypatch)
    token = issue(TG_OWNER)

    body = upload(client, **{"X-Upload-Token": token}).json()

    assert body["ingested"] is True
    assert body["added"] >= 2, body
    assert body["batch_id"] > 0
    assert operations_of(TG_OWNER) >= 2


def test_ссылку_можно_прислать_и_заголовком_authorization(client, monkeypatch):
    """Bearer — то же самое: «Быстрые команды» умеют оба заголовка."""
    vision(monkeypatch, rows=1, merchant="Пекарня")
    token = issue(TG_OWNER)

    body = upload(client, Authorization=f"Bearer {token}").json()

    assert body["ingested"] is True


def test_чужая_ссылка_не_пускает_в_мою_книгу(client, monkeypatch):
    """Ключ соседа заливает соседу, а не мне: это и есть весь смысл."""
    vision(monkeypatch, rows=1, merchant="Чужая лавка")
    stranger_token = issue(TG_STRANGER)
    before = operations_of(TG_OWNER)

    body = upload(client, **{"X-Upload-Token": stranger_token}).json()

    assert body["ingested"] is True
    assert operations_of(TG_STRANGER) >= 1
    assert operations_of(TG_OWNER) == before


def test_неизвестная_ссылка_отвечает_как_несуществующая(client, upload_dir: Path):
    """По ответу нельзя понять, существует такой ключ или нет."""
    before = len(list(upload_dir.iterdir()))

    unknown = upload(client, **{"X-Upload-Token": "fm_" + "z" * 43})
    garbage = upload(client, **{"X-Upload-Token": "fm_" + "q" * 43})

    assert unknown.status_code == 401
    assert garbage.status_code == 401
    assert unknown.json() == garbage.json()
    # Файл по непризнанной ссылке на диске не остаётся.
    assert len(list(upload_dir.iterdir())) == before


def test_отзыв_ломает_старую_ссылку_немедленно(client, monkeypatch):
    vision(monkeypatch, rows=1, merchant="До отзыва")
    old = issue(TG_OWNER)
    assert upload(client, **{"X-Upload-Token": old}).status_code == 200

    new = rotate(TG_OWNER)
    assert new != old

    stale = upload(client, **{"X-Upload-Token": old})
    assert stale.status_code == 401
    # И ровно тот же ответ, что на никогда не существовавший ключ.
    assert stale.json() == upload(client, **{"X-Upload-Token": "fm_" + "z" * 43}).json()

    fresh = upload(client, **{"X-Upload-Token": new})
    assert fresh.status_code == 200
    assert fresh.json()["ingested"] is True


# --- tg_id: теперь только со служебным ключом -------------------------------


def test_tg_id_без_служебного_ключа_игнорируется(client, monkeypatch):
    """Файл принимается, но в чужую книгу не попадает и человека не заводит."""

    def boom(*args, **kwargs):
        raise AssertionError("разбор не должен был запуститься")

    from app.ingest import vision as vision_mod

    monkeypatch.setattr(vision_mod, "parse_images", boom)

    body = upload(client, **{"X-Tg-Id": str(TG_VICTIM)}).json()

    assert set(body) == {"filename", "stored_as", "content_type", "size", "url"}
    assert not user_exists(TG_VICTIM), "по чужому tg_id нельзя даже завести человека"


def test_tg_id_в_query_и_в_форме_тоже_игнорируется(client, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("разбор не должен был запуститься")

    from app.ingest import vision as vision_mod

    monkeypatch.setattr(vision_mod, "parse_images", boom)

    query = client.post(
        "/upload", content=png(), headers={"Content-Type": "image/png"},
        params={"tg_id": TG_VICTIM},
    )
    form = client.post(
        "/upload", files={"file": ("x.png", png(), "image/png")},
        data={"tg_id": str(TG_VICTIM)},
    )

    assert "ingested" not in query.json()
    assert "ingested" not in form.json()
    assert not user_exists(TG_VICTIM)


def test_tg_id_со_служебным_ключом_работает(client, monkeypatch):
    """Заливка за пользователя нашим же сервисом — законный сценарий."""
    vision(monkeypatch, rows=2, merchant="Служебная")

    body = upload(
        client, **{"X-Tg-Id": str(TG_SERVICE), "X-API-Key": SERVICE_KEY}
    ).json()

    assert body["ingested"] is True
    assert user_exists(TG_SERVICE)
    assert operations_of(TG_SERVICE) >= 2


def test_ключ_загрузок_не_заменяет_служебный(client, monkeypatch):
    """UPLOAD_API_KEY отвечает на другой вопрос — «пускать ли вообще».

    Если бы tg_id признавался по нему, то при пустом UPLOAD_API_KEY (а он
    пуст в задеплоенной конфигурации) дыра осталась бы открытой.
    """

    def boom(*args, **kwargs):
        raise AssertionError("разбор не должен был запуститься")

    from app.config import get_settings
    from app.ingest import vision as vision_mod

    monkeypatch.setattr(vision_mod, "parse_images", boom)
    os.environ["UPLOAD_API_KEY"] = "upload-secret"
    get_settings.cache_clear()
    try:
        body = upload(
            client, **{"X-Tg-Id": str(TG_VICTIM), "X-API-Key": "upload-secret"}
        )
        assert body.status_code == 200, body.text
        assert "ingested" not in body.json()
        assert not user_exists(TG_VICTIM)
    finally:
        os.environ["UPLOAD_API_KEY"] = ""
        get_settings.cache_clear()


# --- анонимная загрузка из ТЗ шортката --------------------------------------


def test_анонимная_загрузка_по_прежнему_работает(client, upload_dir: Path):
    """Без ключа /upload остаётся ровно тем, что описано в ТЗ команды."""
    raw = upload(client)
    form = client.post("/upload", files={"file": ("x.png", png(), "image/png")})

    assert raw.status_code == 200, raw.text
    assert form.status_code == 200, form.text
    body = raw.json()
    assert set(body) == {"filename", "stored_as", "content_type", "size", "url"}
    assert (upload_dir / body["stored_as"]).is_file()
    assert client.get(f"/files/{body['stored_as']}").status_code == 200


def test_звук_по_ссылке_не_разбирается_как_картинка(client, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("звук — не картинка")

    from app.asr import transcribe as asr_mod
    from app.ingest import vision as vision_mod

    monkeypatch.setattr(vision_mod, "parse_images", boom)
    # Звук идёт в распознавание речи — своей дорогой, мимо зрения.
    monkeypatch.setattr(asr_mod, "transcribe", lambda *a, **k: "такси 450")
    token = issue(TG_OWNER)

    body = upload(client, m4a(), content_type="audio/mp4", **{"X-Upload-Token": token})

    assert body.status_code == 200, body.text
    data = body.json()
    assert data["ingested"] is True
    assert data["heard"] == "такси 450"


# --- секрет не утекает ------------------------------------------------------


def test_токен_не_попадает_ни_в_ответ_ни_в_список_файлов_ни_в_логи(
    client, monkeypatch, caplog
):
    vision(monkeypatch, rows=1, merchant="Тихая")
    token = issue(TG_OWNER)

    with caplog.at_level(logging.DEBUG):
        response = upload(client, **{"X-Upload-Token": token})

    assert response.status_code == 200, response.text
    body = response.json()
    assert token not in response.text
    assert token not in body["url"], "секрет в ссылке на файл — это ссылка в чужой чат"
    assert token not in body["stored_as"]

    listed = client.get("/files")
    assert listed.status_code == 200
    assert token not in listed.text

    written = "\n".join(record.getMessage() for record in caplog.records)
    assert token not in written, "полный токен в логе обнуляет весь смысл секрета"
    # Зато видно, чья это была загрузка: без этого журнал бесполезен.
    assert "от пользователя" in written


def test_в_логе_видно_отклонённую_ссылку_но_не_её_значение(client, caplog):
    token = "fm_" + "w" * 43
    with caplog.at_level(logging.DEBUG):
        assert upload(client, **{"X-Upload-Token": token}).status_code == 401

    written = "\n".join(record.getMessage() for record in caplog.records)
    assert token not in written
    assert "непризнанной ссылкой" in written


# --- выдача и отзыв ссылки в Mini App ---------------------------------------


def test_mini_app_выдаёт_ссылку_и_повторяет_ту_же(client):
    first = client.get("/api/v1/me/upload-link", headers=debug_auth(TG_OWNER))
    assert first.status_code == 200, first.text
    body = first.json()

    assert body["url"] == f"{BASE_URL}/upload"
    assert body["token"] not in body["url"], "секрету не место в адресе"
    assert body["token"] in body["curl"]
    assert body["issued_at"]

    again = client.get("/api/v1/me/upload-link", headers=debug_auth(TG_OWNER)).json()
    assert again["token"] == body["token"]


def test_mini_app_перевыпускает_ссылку_и_ломает_старую(client, monkeypatch):
    vision(monkeypatch, rows=1, merchant="После отзыва")
    old = client.get("/api/v1/me/upload-link", headers=debug_auth(TG_STRANGER)).json()

    rotated = client.post(
        "/api/v1/me/upload-link/rotate", headers=debug_auth(TG_STRANGER)
    )
    assert rotated.status_code == 200, rotated.text
    new = rotated.json()

    assert new["token"] != old["token"]
    assert upload(client, **{"X-Upload-Token": old["token"]}).status_code == 401
    assert upload(client, **{"X-Upload-Token": new["token"]}).status_code == 200


def test_чужую_ссылку_в_mini_app_не_отдают(client):
    """Каждый видит только свой ключ — ручка ходит по подписи, а не по id."""
    mine = client.get("/api/v1/me/upload-link", headers=debug_auth(TG_OWNER)).json()
    other = client.get("/api/v1/me/upload-link", headers=debug_auth(TG_STRANGER)).json()

    assert mine["token"] != other["token"]
    assert client.get("/api/v1/me/upload-link").status_code == 401


# --- прослеживаемость: файл <-> операции ------------------------------------


def test_по_файлу_видно_какие_операции_он_породил(client, monkeypatch):
    vision(monkeypatch, rows=3, merchant="Прослеживаемая")
    token = issue(TG_OWNER)

    uploaded = upload(client, **{"X-Upload-Token": token}).json()
    batch_id = uploaded["batch_id"]

    detail = client.get(f"/api/v1/me/imports/{batch_id}", headers=debug_auth(TG_OWNER))
    assert detail.status_code == 200, detail.text
    body = detail.json()

    # Запись о загрузке знает, какой именно файл её породил.
    assert body["batch"]["stored_as"] == uploaded["stored_as"]
    assert body["batch"]["file_url"] == uploaded["url"]
    assert body["batch"]["content_type"] == "image/png"
    assert body["batch"]["size"] == uploaded["size"]
    assert body["batch"]["channel"] == "shortcut"
    # И знает, что из него получилось.
    assert body["batch"]["operations"] == len(body["operations"]) >= 3
    assert all(op["title"] for op in body["operations"])
    assert body["batch"]["operations"] == uploaded["added"]


def test_по_операции_видно_из_какого_файла_она_приехала(client, monkeypatch):
    vision(monkeypatch, rows=2, merchant="Обратная связь")
    token = issue(TG_OWNER)
    uploaded = upload(client, **{"X-Upload-Token": token}).json()

    operations = client.get(
        "/api/v1/me/operations?period=2026-09&with_sources=true",
        headers=debug_auth(TG_OWNER),
    )
    assert operations.status_code == 200, operations.text

    from_file = [
        op
        for op in operations.json()
        if any(src["batch_id"] == uploaded["batch_id"] for src in op["sources"])
    ]
    assert len(from_file) >= 2, "операции обязаны помнить свою загрузку"
    source = next(
        src for src in from_file[0]["sources"] if src["batch_id"] == uploaded["batch_id"]
    )
    assert source["stored_as"] == uploaded["stored_as"]
    assert source["file_url"] == uploaded["url"]
    assert source["channel"] == "shortcut"


def test_список_загрузок_показывает_каждый_присланный_файл(client, monkeypatch):
    vision(monkeypatch, rows=1, merchant="В списке")
    token = issue(TG_OWNER)
    uploaded = upload(client, **{"X-Upload-Token": token}).json()

    listed = client.get("/api/v1/me/imports", headers=debug_auth(TG_OWNER))
    assert listed.status_code == 200, listed.text
    rows = listed.json()

    mine = next(row for row in rows if row["id"] == uploaded["batch_id"])
    assert mine["stored_as"] == uploaded["stored_as"]
    assert mine["channel"] == "shortcut"
    # Свежие сверху: человек смотрит сюда сразу после отправки файла.
    assert rows[0]["id"] == uploaded["batch_id"]


def test_чужую_загрузку_не_показывают(client, monkeypatch):
    vision(monkeypatch, rows=1, merchant="Секретная")
    uploaded = upload(client, **{"X-Upload-Token": issue(TG_STRANGER)}).json()

    response = client.get(
        f"/api/v1/me/imports/{uploaded['batch_id']}", headers=debug_auth(TG_OWNER)
    )
    assert response.status_code == 404, response.text


def test_нераспознанная_картинка_тоже_оставляет_след(client, monkeypatch):
    """«Файл приехал, но ничего не вышло» — это ответ, а не молчание."""
    from app.ingest import vision as vision_mod

    monkeypatch.setattr(
        vision_mod,
        "parse_images",
        lambda images, **kwargs: vision_mod.VisionResult("other", [], ""),
    )
    token = issue(TG_OWNER)

    body = upload(client, **{"X-Upload-Token": token}).json()

    assert body["ingested"] is False
    assert body["batch_id"] > 0
    assert body["added"] == 0

    detail = client.get(
        f"/api/v1/me/imports/{body['batch_id']}", headers=debug_auth(TG_OWNER)
    ).json()
    assert detail["batch"]["stored_as"] == body["stored_as"]
    assert detail["operations"] == []


# --- личный файл виден только хозяину ---------------------------------------
#
# Опознание закрыло запись, но не чтение: список файлов сам называл имена
# чужих скриншотов, а ссылка на файл открывалась кому угодно. Половина
# задачи «что именно скинул» оставалась видна всему интернету.


def test_личный_файл_не_отдаётся_по_голому_адресу(client, monkeypatch):
    vision(monkeypatch, rows=1, merchant="Приватная")
    token = issue(TG_OWNER)

    body = upload(client, **{"X-Upload-Token": token}).json()
    name = body["stored_as"]

    # Голый адрес — это ровно то, что раньше отдавало чужой скриншот банка.
    assert client.get(f"/files/{name}").status_code == 401
    # Хозяин открывает свой файл тремя способами: подписанной ссылкой,
    # личным ключом и подписью Mini App.
    assert client.get(body["url"].removeprefix(BASE_URL)).status_code == 200
    assert client.get(f"/files/{name}", headers={"X-Upload-Token": token}).status_code == 200
    # Чужой ключ — не ключ: сосед остаётся снаружи.
    stranger = issue(TG_STRANGER)
    assert client.get(f"/files/{name}", headers={"X-Upload-Token": stranger}).status_code == 401


def test_личного_файла_нет_в_общем_списке(client, monkeypatch):
    """Список сам называл случайные имена — и неугадываемое имя переставало защищать."""
    vision(monkeypatch, rows=1, merchant="Не в списке")
    mine = upload(client, **{"X-Upload-Token": issue(TG_OWNER)}).json()["stored_as"]
    anonymous = upload(client).json()["stored_as"]

    names = [row["name"] for row in client.get("/files").json()["files"]]

    assert mine not in names, "личный файл в публичном списке — это чужой скриншот банка"
    # Анонимная загрузка «по ТЗ» остаётся публичной: чей это файл, мы не знаем.
    assert anonymous in names


def test_подпись_от_одного_файла_не_открывает_другой(client, monkeypatch):
    vision(monkeypatch, rows=1, merchant="Первая")
    first = upload(client, **{"X-Upload-Token": issue(TG_OWNER)}).json()
    vision(monkeypatch, rows=1, merchant="Вторая")
    second = upload(client, **{"X-Upload-Token": issue(TG_STRANGER)}).json()

    query = first["url"].split("?", 1)[1]

    assert client.get(f"/files/{second['stored_as']}?{query}").status_code == 401
    assert client.get(f"/files/{second['stored_as']}?s=1893456000.deadbeef").status_code == 401


def test_mini_app_показывает_превью_своего_файла_и_не_показывает_чужого(client, monkeypatch):
    """Ссылка из /me/imports обязана открываться: тег img заголовков не шлёт."""
    vision(monkeypatch, rows=1, merchant="Превью")
    uploaded = upload(client, **{"X-Upload-Token": issue(TG_OWNER)}).json()

    listed = client.get("/api/v1/me/imports", headers=debug_auth(TG_OWNER)).json()
    mine = next(row for row in listed if row["id"] == uploaded["batch_id"])

    assert "?s=" in mine["file_url"], "без подписи превью в Mini App не откроется"
    assert client.get(mine["file_url"].removeprefix(BASE_URL)).status_code == 200
    # Подпись Mini App — тоже доказательство, но только своя.
    name = uploaded["stored_as"]
    assert client.get(f"/files/{name}", headers=debug_auth(TG_OWNER)).status_code == 200
    assert client.get(f"/files/{name}", headers=debug_auth(TG_STRANGER)).status_code == 401


def test_файлы_с_хозяином_переезжают_в_закрытый_каталог(client, monkeypatch, upload_dir: Path):
    """Уже залитые файлы чинит передеплой, а не человек с ssh."""
    from app.api.uploads import PRIVATE_DIR, secure_stored_files
    from app.config import get_settings

    vision(monkeypatch, rows=1, merchant="Наследство")
    name = upload(client, **{"X-Upload-Token": issue(TG_OWNER)}).json()["stored_as"]

    # Возвращаем файл туда, где он лежал до разделения каталогов.
    (upload_dir / PRIVATE_DIR / name).rename(upload_dir / name)
    assert client.get(f"/files/{name}").status_code == 200, "до починки файл был открыт"

    moved = secure_stored_files(get_settings())

    assert moved >= 1
    assert (upload_dir / PRIVATE_DIR / name).is_file()
    assert client.get(f"/files/{name}").status_code == 401


# --- служебный ключ с кириллицей --------------------------------------------


def test_кириллица_в_служебном_ключе_не_роняет_загрузку(client):
    """`.env.example` раздавал именно такой ключ, а compare_digest на нём падал.

    Проверка служебного ключа идёт теперь на каждой загрузке, поэтому
    неASCII-значение превращало единственную ручку из ТЗ в 500 у всякого,
    кто развернул проект по инструкции и не заменил плейсхолдер.
    """
    from app.config import get_settings

    saved = os.environ.get("INGEST_API_KEY", "")
    os.environ["INGEST_API_KEY"] = "сгенерируйте-свой-ключ"
    get_settings.cache_clear()
    try:
        anonymous = upload(client)
        assert anonymous.status_code == 200, anonymous.text
        assert set(anonymous.json()) == {
            "filename", "stored_as", "content_type", "size", "url"
        }

        # И заодно не превратилось в «пускаем всех»: заголовок HTTP
        # кириллицы не несёт в принципе, поэтому такой ключ не подойдёт
        # никому — но отвечать на это надо отказом, а не пятисоткой.
        wrong = upload(client, **{"X-Tg-Id": str(TG_VICTIM), "X-API-Key": "whatever"})
        assert wrong.status_code == 200
        assert "ingested" not in wrong.json()
        assert not user_exists(TG_VICTIM)
    finally:
        os.environ["INGEST_API_KEY"] = saved
        get_settings.cache_clear()


def test_кириллица_в_ключе_на_запись_отвечает_401_а_не_500(client):
    """Тот же плейсхолдер, но в другом ключе: отказ — это ответ, 500 — нет."""
    from app.config import get_settings

    os.environ["UPLOAD_API_KEY"] = "ключ-на-запись"
    get_settings.cache_clear()
    try:
        assert upload(client).status_code == 401
        assert upload(client, **{"X-API-Key": "guess"}).status_code == 401
    finally:
        os.environ["UPLOAD_API_KEY"] = ""
        get_settings.cache_clear()
        assert upload(client).status_code == 200, "ключ снят — приём снова открыт"


# --- ключ в адресе ----------------------------------------------------------


def test_ключ_в_адресе_работает_но_говорит_об_этом(client, monkeypatch, caplog):
    """Путь из ТЗ остаётся рабочим, но перестаёт быть тихим.

    Полный URL со строкой запроса пишут все прокси подряд, и «поделиться
    ссылкой» отдаёт вместе с ней доступ. Молчать об этом нельзя: человек
    журнал сервера никогда не увидит.
    """
    vision(monkeypatch, rows=1, merchant="Из адреса")
    token = issue(TG_OWNER)

    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/upload", content=png(), headers={"Content-Type": "image/png"},
            params={"token": token},
        )

    body = response.json()
    assert response.status_code == 200, response.text
    assert body["ingested"] is True, "старые команды обязаны продолжать работать"
    assert "X-Upload-Token" in body["warning"]
    assert token not in response.text

    # Только наши записи: httpx в тесте печатает полный адрес запроса — это
    # ровно то, чем плох ключ в адресе, и ровно поэтому мы предупреждаем.
    written = "\n".join(
        record.getMessage() for record in caplog.records if record.name.startswith("app.")
    )
    assert "в адресе" in written
    assert token not in written

    # Заголовком — тот же результат, но без замечания.
    assert "warning" not in upload(client, **{"X-Upload-Token": token}).json()


# --- в логе видно, кого звать ------------------------------------------------


def test_в_логе_отказа_есть_за_что_ухватиться(client, caplog):
    """Запись без tg_id и без файла не даёт сделать ничего из того, ради чего написана."""
    token = "fm_" + "y" * 43
    with caplog.at_level(logging.DEBUG):
        assert upload(client, **{"X-Upload-Token": token}).status_code == 401
        ignored = upload(client, **{"X-Tg-Id": str(TG_VICTIM)}).json()

    written = "\n".join(record.getMessage() for record in caplog.records)

    # По отказу видно, один это человек со старой командой или перебор.
    assert token[:6] in written
    assert token not in written
    # А по игнорированному tg_id — кого звать пересобирать команду.
    assert str(TG_VICTIM) in written
    assert ignored["stored_as"] in written


# --- повторная присылка того же файла ----------------------------------------


def test_повтор_того_же_скриншота_показывает_свои_операции(client, monkeypatch):
    """Самый частый случай: человек фотографирует список раз в неделю.

    Строки-дубли в базу не пишутся, и раньше по такой загрузке не было
    видно ни одной операции — «файл приехал, ничего не вышло» при восьми
    его же операциях на экране.
    """
    vision(monkeypatch, rows=3, merchant="Повторная")
    token = issue(TG_OWNER)

    first = upload(client, **{"X-Upload-Token": token}).json()
    second = upload(client, **{"X-Upload-Token": token}).json()

    assert first["added"] == 3
    assert second["added"] == 0, "новых операций во второй раз действительно нет"
    assert second["duplicates"] == 3
    assert second["recognized"] == 3, "а вот увидеть свои операции человек обязан"

    detail = client.get(
        f"/api/v1/me/imports/{second['batch_id']}", headers=debug_auth(TG_OWNER)
    ).json()
    earlier = client.get(
        f"/api/v1/me/imports/{first['batch_id']}", headers=debug_auth(TG_OWNER)
    ).json()

    assert detail["batch"]["operations"] == 3
    assert len(detail["operations"]) == 3
    assert {op["id"] for op in detail["operations"]} == {op["id"] for op in earlier["operations"]}


def test_карточка_загрузки_показывает_источники_операций(client, monkeypatch):
    """Одна и та же операция обязана выглядеть одинаково в обоих списках."""
    vision(monkeypatch, rows=2, merchant="С источником")
    uploaded = upload(client, **{"X-Upload-Token": issue(TG_OWNER)}).json()

    detail = client.get(
        f"/api/v1/me/imports/{uploaded['batch_id']}", headers=debug_auth(TG_OWNER)
    ).json()

    assert detail["operations"], "загрузка обязана показать свои операции"
    for operation in detail["operations"]:
        assert any(src["batch_id"] == uploaded["batch_id"] for src in operation["sources"])
        assert all(src["file_url"] for src in operation["sources"] if src["stored_as"])


# --- картинка из Mini App тоже сохраняется ----------------------------------


def test_картинка_из_mini_app_остаётся_файлом_с_именем(client, monkeypatch):
    """Бот и Mini App — почти все присылки, и у них карточка была пустой."""
    vision(monkeypatch, rows=1, merchant="Из приложения")

    response = client.post(
        "/api/v1/me/images",
        files={"files": ("shot.png", png(), "image/png")},
        headers=debug_auth(TG_OWNER),
    )
    assert response.status_code == 200, response.text
    batch_id = response.json()["batch_id"]

    batch = client.get(
        f"/api/v1/me/imports/{batch_id}", headers=debug_auth(TG_OWNER)
    ).json()["batch"]

    assert batch["stored_as"], "файл обязан остаться на диске, иначе смотреть нечего"
    assert batch["content_type"] == "image/png"
    assert batch["channel"] == "miniapp"
    # Имя из запроса — единственное, чем две присылки отличаются в списке.
    assert batch["filename"] == "shot.png"
    assert client.get(batch["file_url"].removeprefix(BASE_URL)).status_code == 200
    assert client.get(f"/files/{batch['stored_as']}").status_code == 401


# --- виджет ходит по личной ссылке, а не по служебному ключу ------------------


def test_виджет_работает_по_личной_ссылке(client):
    """Служебный ключ на телефоне открывал запись в книгу любого человека."""
    response = client.post(
        "/api/v1/quick/tap",
        json={"text": "кофе 300", "tg_id": TG_VICTIM},
        headers={"X-Upload-Token": issue(TG_OWNER)},
    )

    assert response.status_code == 200, response.text
    assert "Записал" in response.json()["headline"]
    # Чужой tg_id в теле по личной ссылке ничего не решает.
    assert not user_exists(TG_VICTIM)


def test_виджет_с_непризнанной_ссылкой_не_пускает(client):
    response = client.post(
        "/api/v1/quick/tap",
        json={"text": "кофе 300"},
        headers={"X-Upload-Token": "fm_" + "z" * 43},
    )
    assert response.status_code == 401
    assert client.post("/api/v1/quick/tap", json={"text": "кофе 300"}).status_code == 401


# --- разбор не останавливает сервис -----------------------------------------


async def test_разбор_картинки_не_останавливает_остальные_запросы(client, monkeypatch):
    """Шесть секунд разбора не должны быть шестью секундами простоя.

    В том же процессе живёт бот, а снаружи стоит туннель: синхронный
    разбор в корутине даёт на демонстрации «залил скриншот — приложение
    упало» (502 на соседних запросах).
    """
    import asyncio
    import threading
    import time

    import httpx

    from app.ingest import vision as vision_mod

    started = threading.Event()

    def slow(images, **kwargs):
        started.set()
        time.sleep(1.0)
        return vision_mod.VisionResult("other", [], "")

    monkeypatch.setattr(vision_mod, "parse_images", slow)

    transport = httpx.ASGITransport(app=client.app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as browser:
        # Время считаем от запуска разбора, а не от начала соседнего
        # запроса: заблокированный цикл не даёт даже начать измерение, и
        # секундомер, запущенный позже, показал бы «всё быстро».
        began = time.monotonic()
        busy = asyncio.create_task(
            browser.post(
                "/api/v1/me/images",
                files={"files": ("slow.png", png(), "image/png")},
                headers=debug_auth(TG_OWNER),
            )
        )
        # Ждём начала разбора, отдавая управление циклу. Если цикл занят
        # разбором, вернётся он сюда только после его конца — и это ровно
        # то, что мы измеряем.
        while not started.is_set() and time.monotonic() - began < 10:
            await asyncio.sleep(0)

        alive = await asyncio.wait_for(browser.get("/healthz"), timeout=10)
        waited = time.monotonic() - began

        assert alive.status_code == 200
        assert started.is_set(), "разбор так и не начался"
        assert waited < 0.9, f"соседний запрос ждал разбора {waited:.2f} с"
        assert not busy.done(), "соседний запрос дождался конца разбора, а не обогнал его"
        await busy


def test_кириллица_в_подписях_из_запроса_это_401_а_не_500(client, monkeypatch):
    """Тот же капкан, что и в служебном ключе, но со стороны клиента.

    Подпись ссылки, пропуск карточки и hash из initData приходят из
    запроса. На обычных строках постоянное по времени сравнение падает на
    любом не-ASCII символе, и вместо честного отказа получается 500 —
    достаточно один раз подставить кириллицу в адрес.
    """
    vision(monkeypatch, rows=1, merchant="Подписи")
    name = upload(client, **{"X-Upload-Token": issue(TG_OWNER)}).json()["stored_as"]

    assert client.get(f"/files/{name}?s=1893456000.подпись").status_code == 401
    assert client.get("/api/v1/quick/card?t=1.1893456000.подпись").status_code == 401
    # В заголовок кириллица попадает процентным кодированием: сам
    # заголовок при этом ASCII, а после разбора initData — уже нет.
    cyrillic = quote("подпись")
    assert client.get(
        "/api/v1/me/summary",
        headers={"X-Telegram-Init-Data": f"user=%7B%22id%22%3A1%7D&hash={cyrillic}"},
    ).status_code == 401
