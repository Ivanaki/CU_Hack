"""Загрузка файлов с шортката iPhone: оба способа, форматы, ошибки, обход пути."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.uploads import PRIVATE_DIR

BASE_URL = "https://example.test"
STORED = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]+$")
TG_ID = 555_001


# --- минимальные валидные заголовки файлов ---------------------------------
#
# Настоящие картинки и звук тестам не нужны: сервер определяет формат по
# сигнатуре, а разбор содержимого мы всё равно подменяем. Зато такие
# заготовки видно глазами и они не тянут бинарники в репозиторий.


def png(payload: int = 64) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        + b"\x00" * payload
        + b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def jpeg(payload: int = 64) -> bytes:
    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        + b"\x00" * payload
        + b"\xff\xd9"
    )


def _ftyp(major: bytes, *compatible: bytes) -> bytes:
    box = b"ftyp" + major + b"\x00\x00\x00\x00" + b"".join(compatible)
    return len(box).to_bytes(4, "big") + box


def heic(payload: int = 64) -> bytes:
    # iPhone кладёт в HEIC и общий бренд mif1, и конкретный heic.
    return _ftyp(b"heic", b"mif1", b"heic") + b"\x00\x00\x00\x08mdat" + b"\x00" * payload


def m4a(payload: int = 64) -> bytes:
    # Голосовая заметка iPhone: тот же контейнер, что и mp4, отличается брендом.
    return _ftyp(b"M4A ", b"isom", b"mp42") + b"\x00\x00\x00\x08mdat" + b"\x00" * payload


def ogg(payload: int = 64) -> bytes:
    page = (
        b"OggS"
        + b"\x00"  # версия
        + b"\x02"  # первая страница потока
        + b"\x00" * 8  # granule position
        + b"\x01\x00\x00\x00"  # serial
        + b"\x00" * 4  # номер страницы
        + b"\x00" * 4  # crc
        + b"\x01"  # один сегмент
        + b"\x13"  # длина сегмента — 19 байт OpusHead
    )
    head = b"OpusHead" + b"\x01\x02\x38\x01\x80\xbb\x00\x00\x00\x00\x00"
    return page + head + b"\x00" * payload


FORMATS = {
    "image/png": (png(), ".png"),
    "image/jpeg": (jpeg(), ".jpg"),
    "image/heic": (heic(), ".heic"),
    "audio/mp4": (m4a(), ".m4a"),
    "audio/ogg": (ogg(), ".ogg"),
}


# --- обвязка ---------------------------------------------------------------


@pytest.fixture(scope="module")
def upload_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("uploads")


@pytest.fixture(scope="module")
def client(_isolated_db, upload_dir: Path):
    """Свой каталог загрузок: тесты не должны писать в рабочий data/uploads."""
    os.environ["UPLOAD_DIR"] = str(upload_dir)
    os.environ["PUBLIC_BASE_URL"] = BASE_URL
    os.environ["MAX_UPLOAD_MB"] = "20"
    os.environ["UPLOAD_API_KEY"] = ""

    from app.config import get_settings

    get_settings.cache_clear()
    from app.api.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def post_raw(client, data: bytes, *, content_type: str, **params) -> object:
    """Второй способ из ТЗ: файл прямо в теле запроса."""
    return client.post(
        "/upload", content=data, headers={"Content-Type": content_type}, params=params
    )


def post_form(client, data: bytes, *, name: str = "file", filename: str = "photo.png",
              content_type: str = "image/png", **fields) -> object:
    """Первый способ из ТЗ: multipart с полем file."""
    return client.post("/upload", files={name: (filename, data, content_type)}, data=fields)


# --- оба способа загрузки --------------------------------------------------


def test_multipart_возвращает_json_со_ссылкой(client):
    response = post_form(client, png(), filename="image.png")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["filename"] == "image.png"
    assert STORED.match(body["stored_as"]), body["stored_as"]
    assert body["stored_as"].endswith(".png")
    assert body["content_type"] == "image/png"
    assert body["size"] == len(png())
    assert body["url"] == f"{BASE_URL}/files/{body['stored_as']}"


def test_файл_в_теле_с_именем_в_query(client):
    response = post_raw(client, jpeg(), content_type="image/jpeg", filename="чек.jpg")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["filename"] == "чек.jpg"
    assert body["content_type"] == "image/jpeg"
    assert body["stored_as"].endswith(".jpg")


def test_файл_в_теле_с_именем_в_заголовке(client):
    response = client.post(
        "/upload",
        content=png(),
        headers={"Content-Type": "image/png", "X-Filename": "shortcut.png"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["filename"] == "shortcut.png"


@pytest.mark.parametrize("mime", list(FORMATS))
def test_форматы_из_тз_принимаются_обоими_способами(client, mime):
    data, ext = FORMATS[mime]

    raw = post_raw(client, data, content_type=mime, filename=f"x{ext}")
    assert raw.status_code == 200, raw.text
    assert raw.json()["content_type"] == mime
    assert raw.json()["stored_as"].endswith(ext)

    form = post_form(client, data, filename=f"x{ext}", content_type=mime)
    assert form.status_code == 200, form.text
    assert form.json()["stored_as"].endswith(ext)


@pytest.mark.parametrize("mime", list(FORMATS))
def test_octet_stream_определяется_по_сигнатуре(client, mime):
    """Тип неизвестен клиенту — значит, его определяет содержимое."""
    data, ext = FORMATS[mime]
    response = post_raw(
        client, data, content_type="application/octet-stream", filename="unknown.bin"
    )
    assert response.status_code == 200, response.text
    assert response.json()["content_type"] == mime
    assert response.json()["stored_as"].endswith(ext)


def test_расширение_берётся_из_содержимого_а_не_из_имени(client):
    """Имя файла — ввод пользователя, и оно не решает, чем файл окажется."""
    response = post_raw(
        client, png(), content_type="application/octet-stream", filename="payload.php"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["filename"] == "payload.php"
    assert body["stored_as"].endswith(".png")
    assert body["content_type"] == "image/png"


def test_содержимое_важнее_заявленного_типа(client):
    """Клиент назвал JPEG png-шкой — верим байтам, а не подписи."""
    response = post_form(client, jpeg(), filename="lie.png", content_type="image/png")
    assert response.status_code == 200, response.text
    assert response.json()["content_type"] == "image/jpeg"


def test_синоним_типа_не_подменяется(client):
    """audio/x-m4a и audio/mp4 — один формат, спорить с клиентом не о чем."""
    response = post_raw(client, m4a(), content_type="audio/x-m4a", filename="voice.m4a")
    assert response.status_code == 200, response.text
    assert response.json()["content_type"] == "audio/x-m4a"
    assert response.json()["stored_as"].endswith(".m4a")


# --- ошибки ----------------------------------------------------------------


def test_пустой_файл_отклоняется_400(client):
    assert post_raw(client, b"", content_type="image/png").status_code == 400
    assert post_form(client, b"", filename="empty.png").status_code == 400


def test_больше_лимита_отклоняется_413(client):
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (21 * 1024 * 1024)
    assert post_raw(client, big, content_type="image/png").status_code == 413
    assert post_form(client, big, filename="big.png").status_code == 413


class _StreamRequest:
    """Запрос без Content-Length: размер заранее неизвестен.

    Проверяем чтение напрямую, потому что TestClient сам собирает тело
    целиком перед вызовом приложения и обрыв приёма через него не виден.
    """

    def __init__(self, chunks: int, sent: list[int]) -> None:
        self.headers: dict[str, str] = {}
        self._chunks = chunks
        self._sent = sent

    async def stream(self):
        for index in range(self._chunks):
            self._sent.append(index)
            yield b"\x00" * (1024 * 1024)


async def test_приём_обрывается_на_лимите_а_не_после_всего_тела():
    from fastapi import HTTPException

    from app.api.uploads import _read_raw

    sent: list[int] = []
    with pytest.raises(HTTPException) as failure:
        await _read_raw(_StreamRequest(40, sent), 20 * 1024 * 1024)

    assert failure.value.status_code == 413
    # Двадцать мегабайт приняли, двадцать первый оборвал приём. Остальные
    # девятнадцать сервер не читал и в память не клал.
    assert len(sent) == 21


async def test_поток_multipart_тоже_обрывается_на_лимите():
    from app.api.uploads import _capped, _TooLarge

    sent: list[int] = []

    async def source():
        for index in range(40):
            sent.append(index)
            yield b"\x00" * (1024 * 1024)

    with pytest.raises(_TooLarge):
        async for _ in _capped(source(), 20 * 1024 * 1024):
            pass
    assert len(sent) == 21


@pytest.mark.parametrize(
    "data,content_type",
    [
        (b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"\x00" * 64, "application/octet-stream"),
        (b"GIF89a" + b"\x00" * 64, "application/octet-stream"),
        (b"<html><script>alert(1)</script></html>", "text/html"),
        (_ftyp(b"isom", b"iso2", b"mp41") + b"\x00" * 64, "video/mp4"),
        (_ftyp(b"qt  ", b"qt  ") + b"\x00" * 64, "application/octet-stream"),
    ],
)
def test_неподдерживаемый_формат_отклоняется_415(client, data, content_type):
    assert post_raw(client, data, content_type=content_type).status_code == 415


def test_ложный_заявленный_тип_не_спасает_чужой_формат(client):
    """Назвать mp4-видео картинкой не выйдет: решает сигнатура."""
    video = _ftyp(b"isom", b"iso2", b"mp41") + b"\x00" * 64
    assert post_form(client, video, filename="a.png", content_type="image/png").status_code == 415


# Не-медиа полезные нагрузки: раньше каждая из них проезжала с кодом 200,
# стоило назвать её поддерживаемым типом. Сигнатуры у них снифер не знает,
# и в этом был весь фокус — «не опознали» означало «верим клиенту».
PAYLOADS = {
    "windows_exe": b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00" + b"\x00" * 64,
    "html_со_скриптом": b"<html><body><script>alert(1)</script></body></html>",
    "простой_текст": "просто текст, не картинка и не звук".encode(),
    "zip": b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + b"\x00" * 64,
    "pdf": b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"\x00" * 64,
    "gif": b"GIF89a\x01\x00\x01\x00\x00\xff" + b"\x00" * 64,
    "нули": b"\x00" * 128,
}


@pytest.mark.parametrize("payload", list(PAYLOADS))
@pytest.mark.parametrize("declared", ["image/png", "image/jpeg", "audio/mpeg", "audio/wav"])
def test_заявленный_тип_не_протаскивает_не_медиа_415(client, payload, declared):
    """Сигнатура обязана совпасть у любого типа, а не только у octet-stream.

    Дыра выглядела так: снифер не опознал байты, заявленный тип есть в
    списке — значит, принимаем. Windows-исполняемый файл с заголовком
    `Content-Type: image/png` сохранялся как `<uuid>.png` и получал
    публичную ссылку на домене проекта.
    """
    data = PAYLOADS[payload]
    assert post_raw(client, data, content_type=declared).status_code == 415
    assert post_form(client, data, filename="x.png", content_type=declared).status_code == 415


def test_отклонённый_файл_не_остаётся_на_диске(client, upload_dir: Path):
    """415 обязана означать «не приняли», а не «приняли и не сказали»."""
    before = {p.name for p in upload_dir.iterdir()}
    assert post_raw(client, PAYLOADS["windows_exe"], content_type="image/png").status_code == 415

    assert {p.name for p in upload_dir.iterdir()} == before
    assert all(f["name"] in before for f in client.get("/files").json()["files"])


@pytest.mark.parametrize(
    "declared,data",
    [
        ("audio/mpeg", b"<!DOCTYPE html><script>alert(1)</script>"),
        ("image/png", b"MZ\x90\x00 fake exe"),
        ("audio/wav", b"just some plain text"),
        ("image/heic", b"PK\x03\x04\x14\x00"),
    ],
)
def test_движок_не_верит_заявленному_типу_на_слово(declared, data):
    """Тот же случай на уровне функции: раньше здесь возвращался declared."""
    from fastapi import HTTPException

    from app.api.uploads import resolve_type

    with pytest.raises(HTTPException) as failure:
        resolve_type(declared, data)
    assert failure.value.status_code == 415


def test_настоящий_mp3_по_прежнему_принимается(client):
    """Строгость не должна съесть формат, ради которого её ослабляли.

    У mp3 сигнатур две: тег ID3 и синхрослово фрейма MPEG. Обе снифер
    знает, поэтому отдельная поблажка «верим заявленному audio/mpeg» была
    не нужна и служила только дырой.
    """
    with_tag = b"ID3\x03\x00\x00\x00\x00\x00\x21" + b"\x00" * 64
    bare_frame = b"\xff\xfb\x90\x64" + b"\x00" * 128

    for blob in (with_tag, bare_frame):
        response = post_raw(client, blob, content_type="audio/mpeg", filename="voice.mp3")
        assert response.status_code == 200, response.text
        assert response.json()["content_type"] == "audio/mpeg"
        assert response.json()["stored_as"].endswith(".mp3")


def test_multipart_без_поля_file_отклоняется_422(client):
    response = post_form(client, png(), name="attachment", filename="photo.png")
    assert response.status_code == 422


def test_испорченный_multipart_отклоняется_400(client):
    response = client.post(
        "/upload",
        content="--nope\r\nбез границ и заголовков\r\n".encode(),
        headers={"Content-Type": "multipart/form-data"},
    )
    assert response.status_code == 400


# --- список и выдача -------------------------------------------------------


def test_список_показывает_загруженное(client):
    stored = post_form(client, png(), filename="in-list.png").json()["stored_as"]

    body = client.get("/files").json()
    row = next((f for f in body["files"] if f["name"] == stored), None)
    assert row is not None, "загруженный файл обязан быть в списке"
    assert row["size"] == len(png())
    assert row["url"] == f"{BASE_URL}/files/{stored}"
    assert all(STORED.match(f["name"]) for f in body["files"])


def test_файл_отдаётся_байт_в_байт_и_не_исполняется_как_html(client):
    blob = jpeg(payload=128)
    stored = post_raw(client, blob, content_type="image/jpeg", filename="a.jpg").json()["stored_as"]

    response = client.get(f"/files/{stored}")
    assert response.status_code == 200
    assert response.content == blob
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == f'inline; filename="{stored}"'


def test_несуществующий_файл_это_404(client):
    assert client.get("/files/" + "0" * 32 + ".png").status_code == 404


def test_файлы_переживают_перезапуск_приложения(client, upload_dir: Path):
    """Каталог загрузок — настройка, а не состояние процесса."""
    stored = post_form(client, png(), filename="survivor.png").json()["stored_as"]
    assert (upload_dir / stored).is_file()

    from app.api.app import create_app

    with TestClient(create_app()) as fresh:
        assert any(f["name"] == stored for f in fresh.get("/files").json()["files"])
        assert fresh.get(f"/files/{stored}").status_code == 200


# --- обход пути ------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "..%2F..%2Fetc%2Fpasswd",
        "%2e%2e%2fconftest.py",
        "../../tests/conftest.py",
        "/etc/passwd",
        "C:%5CWindows%5Cwin.ini",
        "0" * 32 + ".png%00.txt",
        "abc%00.png",
        "....//" + "0" * 32 + ".png",
        "0" * 31 + ".png",  # 31 символ вместо 32
        "0" * 32 + ".exe",
        "0" * 32 + ".png.exe",
        "0" * 32 + ".png%0A",  # перевод строки в конце имени — инъекция заголовка
        "0" * 32 + ".png%0D%0AX-Evil:%201",
        "ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ.png",  # не hex
    ],
)
def test_обход_пути_не_проходит(client, name):
    response = client.get(f"/files/{name}")
    assert response.status_code in (400, 404), response.text
    assert b"root:" not in response.content
    assert b"conftest" not in response.content


def test_имя_из_запроса_не_становится_путём(client, upload_dir: Path):
    response = post_raw(
        client, png(), content_type="image/png", filename="../../../evil.png"
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # Имя вернулось клиенту как есть, но на диск попало наше собственное.
    assert body["filename"] == "../../../evil.png"
    assert STORED.match(body["stored_as"])
    assert (upload_dir / body["stored_as"]).is_file()
    assert not (upload_dir.parent.parent / "evil.png").exists()


def test_имя_с_нулевым_байтом_не_ломает_загрузку(client, upload_dir: Path):
    response = client.post(
        "/upload",
        content=png(),
        headers={"Content-Type": "image/png", "X-Filename": "a\x00b.png"},
    )
    assert response.status_code == 200, response.text
    stored = response.json()["stored_as"]
    assert STORED.match(stored)
    assert (upload_dir / stored).is_file()


# --- разбор картинки -------------------------------------------------------


def _fake_vision(monkeypatch, rows: int = 2):
    from datetime import datetime

    from app.ingest import vision
    from app.ingest.base import ParsedRow

    def parse_images(images, **kwargs):
        assert images and images[0].startswith(b"\x89PNG")
        return vision.VisionResult(
            "statement",
            [
                ParsedRow(
                    booked_at=datetime(2026, 9, 12, 10, index),
                    amount=-45000 - index,
                    description=f"Кофейня {index}",
                    bank="Т-Банк",
                )
                for index in range(rows)
            ],
            "Т-Банк",
        )

    monkeypatch.setattr(vision, "parse_images", parse_images)


@pytest.fixture(scope="module")
def token(client) -> str:
    """Личная ссылка пользователя: теперь именно она говорит, чей это файл.

    tg_id из запроса больше не опознаёт человека — он не секрет, и по нему
    кто угодно залил бы операции в чужую книгу (см. test_upload_identity).
    """
    from app.db import session_scope
    from app.service import get_or_create_user, issue_upload_token

    with session_scope() as session:
        return issue_upload_token(session, get_or_create_user(session, TG_ID, "Шорткат"))


def test_без_токена_ответ_ровно_по_тз(client):
    """Клиент из ТЗ не должен увидеть ничего лишнего."""
    body = post_form(client, png(), filename="plain.png").json()
    assert set(body) == {"filename", "stored_as", "content_type", "size", "url"}


def test_картинка_по_личной_ссылке_разбирается_в_операции(client, monkeypatch, token):
    _fake_vision(monkeypatch)
    response = post_raw(
        client, png(), content_type="image/png", filename="shot.png", token=token
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # Обязательные поля из ТЗ на месте и не изменились.
    assert body["content_type"] == "image/png"
    assert body["url"].startswith(f"{BASE_URL}/files/")
    # И появились необязательные.
    assert body["ingested"] is True
    assert body["operations"] >= 2
    assert isinstance(body["questions"], int)

    users = client.get(
        "/api/v1/ingest/users", headers={"X-API-Key": os.environ.get("INGEST_API_KEY", "")}
    )
    if users.status_code == 200:
        assert any(u["tg_id"] == TG_ID for u in users.json())


def test_токен_можно_прислать_полем_формы(client, monkeypatch, token):
    _fake_vision(monkeypatch, rows=1)
    response = post_form(client, png(), filename="form.png", token=token)
    assert response.status_code == 200, response.text
    assert response.json()["ingested"] is True


def test_разбор_можно_выключить(client, monkeypatch, token):
    def boom(*args, **kwargs):
        raise AssertionError("разбор не должен был запуститься")

    from app.ingest import vision

    monkeypatch.setattr(vision, "parse_images", boom)
    body = post_raw(
        client, png(), content_type="image/png", token=token, analyze="false"
    ).json()
    assert "ingested" not in body


def test_файл_без_разбора_всё_равно_принадлежит_хозяину(client, monkeypatch, token):
    """Разбор выключен — но файл лежит в закрытом каталоге и обязан открыться.

    Хозяин файла находится единственным способом: по записи о загрузке.
    Если её не создать, закрытый каталог не отдаст человеку его же файл,
    а ссылку на этот файл мы ему уже вернули.
    """
    from app.ingest import vision

    monkeypatch.setattr(vision, "parse_images", lambda *a, **k: pytest.fail("не звать"))
    body = post_raw(
        client, png(), content_type="image/png", token=token, analyze="false"
    ).json()

    assert client.get(body["url"].removeprefix(BASE_URL)).status_code == 200
    # И по личному ключу тоже: подпись живёт недолго, ключ — постоянно.
    plain = client.get(
        f"/files/{body['stored_as']}", headers={"X-Upload-Token": token}
    )
    assert plain.status_code == 200


def test_звук_хозяина_открывается_хозяином(client, token):
    """Звук не проходит через разбор картинок — и тоже мог остаться ничьим."""
    body = post_raw(client, m4a(), content_type="audio/mp4", token=token).json()
    assert client.get(body["url"].removeprefix(BASE_URL)).status_code == 200
    assert client.get(f"/files/{body['stored_as']}").status_code == 401


def test_звук_по_ссылке_не_идёт_в_разбор_картинок(client, monkeypatch, token):
    def boom(*args, **kwargs):
        raise AssertionError("звук — не картинка")

    from app.ingest import vision

    monkeypatch.setattr(vision, "parse_images", boom)
    body = post_raw(client, m4a(), content_type="audio/mp4", token=token).json()
    assert body["content_type"] == "audio/mp4"
    assert "ingested" not in body


def test_ошибка_разбора_не_ломает_загрузку(client, monkeypatch, upload_dir: Path, token):
    from app.ingest import vision

    def explode(*args, **kwargs):
        raise RuntimeError("модель не ответила")

    monkeypatch.setattr(vision, "parse_images", explode)
    response = post_raw(client, png(), content_type="image/png", token=token)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ingested"] is False
    # Файл всё равно сохранён и доступен: загрузка не зависит от разбора.
    # Лежит он в закрытом подкаталоге — это файл опознанного человека, — и
    # открывается по подписанной ссылке из того же ответа.
    assert (upload_dir / PRIVATE_DIR / body["stored_as"]).is_file()
    assert client.get(body["url"].removeprefix(BASE_URL)).status_code == 200


def test_выключенная_модель_это_не_ошибка_загрузки(client, token):
    """LLM_PROVIDER=none — штатный путь: файл принят, операций не добавилось."""
    body = post_raw(client, png(), content_type="image/png", token=token).json()
    assert body["ingested"] is False
    assert STORED.match(body["stored_as"])


# --- ключ и CORS -----------------------------------------------------------


def test_ключ_на_запись_если_задан_обязателен(client):
    from app.config import get_settings

    os.environ["UPLOAD_API_KEY"] = "upload-secret"
    get_settings.cache_clear()
    try:
        assert post_form(client, png(), filename="x.png").status_code == 401
        assert client.get("/files").status_code == 401

        ok = client.post(
            "/upload",
            files={"file": ("x.png", png(), "image/png")},
            headers={"X-API-Key": "upload-secret"},
        )
        assert ok.status_code == 200, ok.text
        # Ссылка обязана открываться без ключа: иначе она бесполезна.
        assert client.get(f"/files/{ok.json()['stored_as']}").status_code == 200
    finally:
        os.environ["UPLOAD_API_KEY"] = ""
        get_settings.cache_clear()


def test_cors_разрешает_запрос_из_браузера(client):
    response = client.options(
        "/upload",
        headers={
            "Origin": "https://example.org",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-filename",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"


# --- публичная ссылка ------------------------------------------------------


def test_ссылка_за_туннелем_остаётся_https(client):
    """Боевая схема без PUBLIC_BASE_URL: снаружи https, внутрь приходит http.

    uvicorn доверяет X-Forwarded-Proto только с 127.0.0.1, а cloudflared в
    docker приходит с адреса сети контейнеров, поэтому request.base_url
    отдавал http на https-входе — ссылка с mixed content для Mini App.
    """
    from app.config import get_settings

    os.environ["PUBLIC_BASE_URL"] = ""
    get_settings.cache_clear()
    proxy = {
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "look-own-alt-electronics.trycloudflare.com",
    }
    try:
        response = client.post(
            "/upload", content=png(), headers={"Content-Type": "image/png", **proxy}
        )
        assert response.status_code == 200, response.text
        stored = response.json()["stored_as"]
        expected = f"https://look-own-alt-electronics.trycloudflare.com/files/{stored}"
        assert response.json()["url"] == expected

        listed = client.get("/files", headers=proxy).json()["files"]
        assert next(f["url"] for f in listed if f["name"] == stored) == expected
    finally:
        os.environ["PUBLIC_BASE_URL"] = BASE_URL
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "host", ["", "evil.test/../x", "evil test", "evil.test:99999999", "evil.test/files/x.png"]
)
def test_мусор_в_x_forwarded_host_в_ссылку_не_попадает(client, host):
    from app.config import get_settings

    os.environ["PUBLIC_BASE_URL"] = ""
    get_settings.cache_clear()
    try:
        response = client.post(
            "/upload",
            content=png(),
            headers={
                "Content-Type": "image/png",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": host,
            },
        )
        assert response.status_code == 200, response.text
        url = response.json()["url"]
        assert url == f"https://testserver/files/{response.json()['stored_as']}"
    finally:
        os.environ["PUBLIC_BASE_URL"] = BASE_URL
        get_settings.cache_clear()


def test_настройка_публичного_адреса_важнее_заголовков(client):
    """Заголовки прокси — запасной путь, а не способ подменить ссылку."""
    response = client.post(
        "/upload",
        content=png(),
        headers={
            "Content-Type": "image/png",
            "X-Forwarded-Proto": "http",
            "X-Forwarded-Host": "attacker.test",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["url"].startswith(f"{BASE_URL}/files/")


def test_боевой_env_задаёт_публичный_адрес():
    """Без PUBLIC_BASE_URL в .env ссылка на боевом стенде уезжает в http."""
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.is_file():
        pytest.skip("на этой машине нет .env — проверять нечего")

    values = dict(
        line.split("=", 1)
        for line in env.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    assert "PUBLIC_BASE_URL" in values, "боевой .env обязан задавать внешний адрес"
    assert values["PUBLIC_BASE_URL"].strip().startswith("https://"), values["PUBLIC_BASE_URL"]


# --- определение формата отдельно ------------------------------------------


@pytest.mark.parametrize("mime", list(FORMATS))
def test_сигнатура_распознаётся_без_имени_и_заголовков(mime):
    from app.api.uploads import sniff_mime

    assert sniff_mime(FORMATS[mime][0][:512]) == mime


@pytest.mark.parametrize(
    "data",
    [b"", b"not a file at all", b"GIF89a", b"\xff\xf1\x50\x80", b"PK\x03\x04"],
)
def test_чужие_сигнатуры_не_опознаются(data):
    from app.api.uploads import sniff_mime

    assert sniff_mime(data) not in FORMATS
