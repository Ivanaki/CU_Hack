"""Приём файлов с шортката iPhone.

Двойное касание по задней панели телефона делает скриншот или голосовую
запись и отправляет файл сюда. Это вход для данных пользователя, а не
файлохранилище: если рядом с картинкой пришёл `tg_id`, мы тут же пробуем
разобрать её в операции тем же движком, что и остальную заливку.

Три вещи, из-за которых модуль выглядит сложнее, чем «сохрани файл»:

* тело читается потоком и обрывается на превышении лимита — иначе любой
  желающий заставил бы сервер сначала принять гигабайт, а потом ответить 413;
* имя из запроса никогда не участвует в построении пути, а расширение берётся
  из типа, определённого по содержимому: присланное имя — это ввод, которому
  нельзя верить;
* отдача файла закрыта двумя независимыми проверками и запретом на
  интерпретацию содержимого браузером.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.api.auth import db_session
from app.config import Settings, get_settings
from app.service import get_or_create_user, ingest_screenshot

log = logging.getLogger(__name__)

uploads = APIRouter(tags=["Загрузки с шортката"])

# Типы из ТЗ шортката и расширение, под которым файл ляжет на диск.
# Расширение берём отсюда, а не из присланного имени: имя — это ввод
# пользователя, а тип мы определили сами.
ALLOWED_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/webm": ".weba",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
}

# Обратная таблица для отдачи файла: расширение → тип. Первый тип в
# ALLOWED_TYPES выигрывает, поэтому .m4a отдаётся как audio/mp4.
_TYPE_BY_EXT: dict[str, str] = {}
for _mime, _ext in ALLOWED_TYPES.items():
    _TYPE_BY_EXT.setdefault(_ext, _mime)

# Строгая регулярка для имени на диске: 32 hex-символа и известное
# расширение. Всё, что сгенерировали не мы, сюда не подходит по построению.
# Конец строки — \Z, а не $: $ пропускает завершающий перевод строки, а имя
# уходит в заголовок Content-Disposition, где перевод строки означает уже
# следующий заголовок.
_STORED_NAME = re.compile(
    r"^[0-9a-f]{32}\.(?:" + "|".join(sorted(e.lstrip(".") for e in _TYPE_BY_EXT)) + r")\Z"
)

# Хост из X-Forwarded-Host: имя, при желании с портом, и ничего больше.
_FORWARDED_HOST = re.compile(r"^[A-Za-z0-9.\-]{1,253}(?::\d{1,5})?\Z")

# Разные имена одного и того же формата. Если клиент назвал тип одним именем
# из группы, а сигнатура дала другое, спорить не о чем: подменять заявленный
# тип синонимом — только сбивать клиента с толку.
_SYNONYMS: tuple[frozenset[str], ...] = (
    frozenset({"image/heic", "image/heif"}),
    frozenset({"audio/mp4", "audio/x-m4a"}),
    frozenset({"audio/wav", "audio/x-wav"}),
)

# Контейнер в заголовке не говорит, есть ли внутри видеодорожка. Шорткат
# присылает только звук, а оба типа есть в списке поддерживаемых, поэтому
# приводим к звуковому имени, вместо того чтобы отвечать 415 на свой же файл.
_CONTAINER_ALIASES = {
    "video/webm": "audio/webm",
    "application/ogg": "audio/ogg",
}

# Multipart добавляет к файлу границы и заголовки частей. Слишком строгий
# лимит на всё тело отверг бы законный файл ровно в 20 МБ.
_MULTIPART_OVERHEAD = 64 * 1024

_TOO_BIG = "файл больше допустимого размера"
_NOT_FOUND = "файл не найден"

# Коды ошибок из ТЗ. Эти два — числами: starlette уже переименовывал свои
# константы для них и ругается на старые имена, а номера из RFC не менялись.
_CODE_TOO_LARGE = 413
_CODE_NO_FILE_FIELD = 422


class _TooLarge(Exception):
    """Внутренний сигнал «хватит читать», чтобы не путать его с ошибкой формы."""


class UploadOut(BaseModel):
    """Ответ на загрузку. Первые пять полей — обязательные, они есть всегда."""

    filename: str = Field(description="Имя, как его прислал клиент")
    stored_as: str = Field(description="Имя на диске: UUID и расширение по типу")
    content_type: str = Field(description="Тип, определённый сервером")
    size: int = Field(description="Размер в байтах")
    url: str = Field(description="Ссылка на файл")

    # Необязательная часть: появляется, только если пришёл tg_id и файл —
    # картинка. Клиент, написанный строго по ТЗ, этих полей не увидит:
    # ответ отдаётся с response_model_exclude_none.
    ingested: bool | None = Field(default=None, description="Удалось ли прочитать операции")
    operations: int | None = Field(
        default=None, description="Операций у пользователя после разбора"
    )
    questions: int | None = Field(default=None, description="Открытых вопросов после разбора")


class FileOut(BaseModel):
    name: str
    size: int
    url: str


class FileListOut(BaseModel):
    files: list[FileOut]


# --------------------------------------------------------------------------
# Определение формата по сигнатуре содержимого
# --------------------------------------------------------------------------

_ISOBMFF_BRANDS = {
    b"heic": "image/heic", b"heix": "image/heic",
    b"heim": "image/heif", b"heis": "image/heif",
    b"hevc": "image/heic-sequence", b"hevx": "image/heic-sequence",
    b"hevm": "image/heif-sequence", b"hevs": "image/heif-sequence",
    b"mif1": "image/heif", b"mif2": "image/heif", b"msf1": "image/heif-sequence",
    b"avif": "image/avif", b"avis": "image/avif-sequence",
    b"M4A ": "audio/mp4", b"M4B ": "audio/mp4", b"M4P ": "audio/mp4",
    b"F4A ": "audio/mp4", b"F4B ": "audio/mp4",
    b"M4V ": "video/x-m4v", b"M4VH": "video/x-m4v", b"M4VP": "video/x-m4v",
    b"qt  ": "video/quicktime",
    b"isom": "video/mp4", b"iso2": "video/mp4", b"iso4": "video/mp4",
    b"iso5": "video/mp4", b"iso6": "video/mp4", b"iso8": "video/mp4",
    b"mp41": "video/mp4", b"mp42": "video/mp4", b"mp71": "video/mp4",
    b"mmp4": "video/mp4", b"avc1": "video/mp4", b"dash": "video/mp4",
}
# Общие бренды пишут в файл все подряд: iPhone кладёт в HEIC и `mif1`, и
# `heic`. Конкретный бренд всегда важнее общего, поэтому общие держим
# отдельным списком и берём их только как запасной ответ.
_ISOBMFF_GENERIC = {
    b"mif1", b"mif2", b"msf1", b"isom", b"iso2", b"iso4", b"iso5", b"iso6",
    b"iso8", b"mp41", b"mp42", b"mp71", b"mmp4", b"avc1", b"dash",
}


def sniff_mime(data: bytes) -> str | None:
    """Тип файла по первым байтам. None — не опознали.

    Имя файла и заявленный Content-Type здесь сознательно не участвуют:
    и то, и другое приходит от клиента и ничего не доказывает.
    """
    b = bytes(data)
    n = len(b)

    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if b.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"

    if n >= 12 and b.startswith(b"RIFF"):
        form = b[8:12]
        if form == b"WEBP":
            return "image/webp"
        if form == b"WAVE":
            return "audio/wav"
        return None
    if n >= 12 and b.startswith(b"RF64") and b[8:12] == b"WAVE":
        return "audio/wav"

    # ISO-BMFF: 'ftyp' на смещении 4, главный бренд на 8, совместимые с 16.
    if n >= 12 and b[4:8] == b"ftyp":
        box_size = int.from_bytes(b[0:4], "big")
        if box_size < 16 or box_size > n:
            box_size = n  # заголовок обрезан — читаем, сколько есть
        brands = [b[8:12]]
        off = 16
        while off + 4 <= box_size:
            brands.append(b[off : off + 4])
            off += 4
        fallback = None
        for brand in brands:
            mime = _ISOBMFF_BRANDS.get(brand)
            if mime is None:
                continue
            if brand not in _ISOBMFF_GENERIC:
                return mime
            if fallback is None:
                fallback = mime
        return fallback

    if b.startswith(b"ID3") and n >= 10 and b[3] < 0xFF and b[4] < 0xFF:
        return "audio/mpeg"
    if n >= 2 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0:
        version = (b[1] >> 3) & 0x03  # 01 — зарезервировано
        layer = (b[1] >> 1) & 0x03  # 01 — Layer III
        if version != 0b01 and layer == 0b01:
            return "audio/mpeg"
        return None  # сюда попадает, например, AAC ADTS (FF F1/FF F9)

    if n >= 5 and b.startswith(b"OggS") and b[4] == 0x00:
        if n >= 27:
            payload = 27 + b[26]  # 27 байт заголовка страницы плюс таблица сегментов
            head = b[payload : payload + 8]
            if head.startswith((b"\x01vorbis", b"OpusHead", b"\x7fFLAC", b"Speex   ")):
                return "audio/ogg"
            if head.startswith((b"\x80theora", b"\x01video\x00")):
                return "video/ogg"
        return "application/ogg"

    if b.startswith(b"\x1aE\xdf\xa3"):
        head = b[:64]
        pos = head.find(b"\x42\x82")  # идентификатор элемента DocType
        if pos != -1 and pos + 2 < len(head):
            size_byte = head[pos + 2]
            if 0x80 < size_byte <= 0xFF:  # однобайтовая длина VINT
                doctype = head[pos + 3 : pos + 3 + (size_byte & 0x7F)].rstrip(b"\x00")
                if doctype == b"webm":
                    return "video/webm"
                if doctype == b"matroska":
                    return "video/x-matroska"
        if b"webm" in head:
            return "video/webm"
        return None

    return None


def _family(mime: str) -> frozenset[str]:
    for group in _SYNONYMS:
        if mime in group:
            return group
    return frozenset({mime})


def resolve_type(declared: str, data: bytes) -> str:
    """Итоговый тип файла. 415, если он не из списка поддерживаемых.

    Решает содержимое, и только оно. У всех одиннадцати типов из ТЗ есть
    сигнатура в начале файла, поэтому неопознанные байты — это не
    «экзотический, но валидный файл», а файл не из списка. Заявленный тип
    ничего в список не добавляет: он лишь выбирает имя, когда называет тот
    же формат другим словом (`audio/x-m4a` и `audio/mp4`).

    Раньше неопознанная сигнатура означала «верим клиенту на слово», и
    строгой проверке подвергался только `application/octet-stream`. Из-за
    этого HTML, обычный текст и даже windows-исполняемый файл проезжали как
    `image/png`: снифер молчит, заявленный тип в списке — файл принят.
    Цена строгости — надуманный mp3, у которого в первых байтах нет ни тега
    ID3, ни фрейма MPEG; такой файл теперь честно получает 415.
    """
    sniffed = sniff_mime(data[:512])
    sniffed = _CONTAINER_ALIASES.get(sniffed or "", sniffed)

    if sniffed is None:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "не удалось определить формат файла по содержимому",
        )

    if declared in ALLOWED_TYPES and declared in _family(sniffed):
        resolved = declared
    else:
        resolved = sniffed

    if resolved not in ALLOWED_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"формат {resolved} не поддерживается"
        )
    return resolved


# --------------------------------------------------------------------------
# Чтение тела запроса
# --------------------------------------------------------------------------


def _reject_by_content_length(request: Request, cap: int) -> None:
    """Отказ до чтения тела: клиент сам сказал, сколько собирается прислать."""
    raw = request.headers.get("content-length") or ""
    if raw.isdigit() and int(raw) > cap:
        raise HTTPException(_CODE_TOO_LARGE, _TOO_BIG)


async def _capped(stream: AsyncGenerator[bytes, None], cap: int) -> AsyncGenerator[bytes, None]:
    """Отдаёт поток дальше, пока не превышен лимит, и обрывает приём на нём."""
    seen = 0
    async for chunk in stream:
        seen += len(chunk)
        if seen > cap:
            raise _TooLarge
        yield chunk


async def _read_raw(request: Request, limit: int) -> bytes:
    """Файл прямо в теле запроса."""
    _reject_by_content_length(request, limit)
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > limit:
            # Дочитывать нечего: ответ уже известен, а память тратить не на что.
            raise HTTPException(_CODE_TOO_LARGE, _TOO_BIG)
    return bytes(buf)


async def _read_multipart(request: Request, limit: int) -> tuple[bytes, str, str, dict[str, str]]:
    """Файл в поле «file» формы. Возвращает данные, имя, тип части и поля формы.

    Парсер собираем руками, а не через `await request.form()`: штатный разбор
    FastAPI не ограничивает размер файловой части и сложил бы на диск столько,
    сколько прислали.
    """
    cap = limit + _MULTIPART_OVERHEAD
    _reject_by_content_length(request, cap)
    parser = MultiPartParser(request.headers, _capped(request.stream(), cap))
    try:
        form = await parser.parse()
    except _TooLarge:
        raise HTTPException(_CODE_TOO_LARGE, _TOO_BIG) from None
    except MultiPartException as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"испорченная форма: {exc.message}"
        ) from exc

    try:
        part = form.get("file")
        if not isinstance(part, UploadFile):
            raise HTTPException(
                _CODE_NO_FILE_FIELD, "в multipart нет файлового поля file"
            )
        data = await part.read()
        fields = {key: value for key, value in form.multi_items() if isinstance(value, str)}
        declared = (part.content_type or "").split(";", 1)[0].strip().lower()
        return data, part.filename or "", declared, fields
    finally:
        await form.close()  # временные файлы парсера нам больше не нужны


# --------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------


def _clean_name(raw: str) -> str:
    """Чистит имя для показа в ответе. В путь оно всё равно не попадает."""
    if not raw:
        return ""
    try:
        # Заголовки HTTP приходят latin-1, а шорткат шлёт utf-8: без этого
        # «чек.png» превратился бы в мусор.
        raw = raw.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    return "".join(ch for ch in raw if ch.isprintable())[:255].strip()


def _as_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _as_tg_id(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "tg_id должен быть числом") from exc


def _base_from_request(request: Request) -> str:
    """Внешний адрес по заголовкам прокси — запасной путь, если настройки нет.

    request.base_url сам по себе врёт за туннелем: внутрь запрос приходит по
    http, и ссылка получается http на https-входе. Подставить схему из
    X-Forwarded-Proto uvicorn бы мог, но по умолчанию доверяет этим
    заголовкам только с 127.0.0.1, а cloudflared в docker приходит с адреса
    сети контейнеров — то есть ровно в боевой конфигурации заголовок
    игнорируется. Читаем его сами: значение уходит в JSON тому же клиенту,
    который его прислал, и никакой другой запрос не затрагивает.
    """
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto not in {"http", "https"}:
        return str(request.base_url)
    host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    # В ссылку пускаем только то, что похоже на имя хоста: она хоть и
    # возвращается автору запроса, но остаётся частью нашего ответа.
    if not _FORWARDED_HOST.match(host):
        host = request.url.netloc
    return f"{proto}://{host}/"


def _public_url(settings: Settings, request: Request, name: str) -> str:
    """Ссылка на файл.

    Базовый адрес берём из настройки: снаружи сервис стоит за туннелем и
    приходит по https, а внутрь запрос попадает по http. Настройки нет —
    собираем адрес из заголовков прокси, см. `_base_from_request`.
    """
    base = settings.public_base_url or _base_from_request(request)
    return f"{base.rstrip('/')}/files/{name}"


def _store(directory: Path, data: bytes, ext: str) -> str:
    """Кладёт файл под своим именем и возвращает его."""
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    tmp = directory / f".{name}.part"
    tmp.write_bytes(data)
    # Переименование атомарно: в списке файлов не может появиться половина.
    os.replace(tmp, directory / name)
    return name


async def _analyze(session: Session, tg_id: int, display_name: str, data: bytes) -> dict[str, Any]:
    """Прогоняет картинку через обычный разбор скриншотов.

    Ошибка разбора не должна ломать загрузку: файл уже на диске, и клиент
    ждёт ссылку, а не отчёт о том, что модель не ответила.
    """

    def run() -> Any:
        user = get_or_create_user(session, tg_id, display_name)
        return ingest_screenshot(session, user, [data])

    try:
        # В этом же процессе живёт бот, а разбор ходит в сеть и считает
        # полный пересчёт. В корутине он остановил бы и бота, и API.
        outcome = await asyncio.to_thread(run)
    except Exception:
        log.exception("разбор картинки с шортката не удался")
        # Сессия могла остаться в нерабочем состоянии: откатываем сами,
        # иначе общий commit после ответа упадёт уже на сохранённом файле.
        session.rollback()
        return {"ingested": False, "operations": None, "questions": None}
    return {
        "ingested": bool(outcome.ok),
        "operations": outcome.operations,
        "questions": outcome.questions,
    }


def require_upload_key(
    settings: Settings = Depends(get_settings),
    x_api_key: str = Header(default="", alias="X-API-Key"),
) -> None:
    """Необязательный ключ на запись.

    В ТЗ шортката ключа нет, поэтому пустой UPLOAD_API_KEY оставляет приём
    открытым. На публичном адресе ключ стоит задать: иначе залить 20 МБ
    может кто угодно, а шорткат заголовок отправить умеет.
    """
    if not settings.upload_api_key:
        return
    if not hmac.compare_digest(x_api_key, settings.upload_api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "неверный X-API-Key")


# --------------------------------------------------------------------------
# Эндпоинты
# --------------------------------------------------------------------------


@uploads.post(
    "/upload",
    response_model=UploadOut,
    response_model_exclude_none=True,
    summary="Принять файл с шортката iPhone",
)
async def upload_file(
    request: Request,
    filename: str = Query(default="", description="Имя файла, если он в теле запроса"),
    tg_id: int | None = Query(default=None, description="Кому засчитать картинку"),
    display_name: str = Query(default=""),
    analyze: bool = Query(default=True, description="Разбирать картинку в операции"),
    x_filename: str = Header(default="", alias="X-Filename"),
    x_tg_id: str = Header(default="", alias="X-Tg-Id"),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(db_session),
    _key: None = Depends(require_upload_key),
) -> UploadOut:
    """Два способа прислать файл: форма с полем «file» или файл прямо в теле.

    Второй способ — ради шортката: он умеет POST-запрос с файлом в теле и
    заголовками, но не умеет собирать multipart.
    """
    limit = settings.max_upload_bytes
    declared_request = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()

    if declared_request.startswith("multipart/"):
        data, part_name, declared, fields = await _read_multipart(request, limit)
    else:
        data = await _read_raw(request, limit)
        part_name, declared, fields = "", declared_request, {}

    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "пустой файл")
    if len(data) > limit:
        raise HTTPException(_CODE_TOO_LARGE, _TOO_BIG)

    content_type = resolve_type(declared, data)
    stored_as = _store(settings.upload_path, data, ALLOWED_TYPES[content_type])
    shown_name = _clean_name(part_name or filename or x_filename) or stored_as

    result = UploadOut(
        filename=shown_name,
        stored_as=stored_as,
        content_type=content_type,
        size=len(data),
        url=_public_url(settings, request, stored_as),
    )

    target = _as_tg_id(fields.get("tg_id")) or tg_id or _as_tg_id(x_tg_id)
    wanted = analyze and _as_bool(fields.get("analyze"), True)
    if target is not None and wanted and content_type.startswith("image/"):
        name = _clean_name(fields.get("display_name", "")) or display_name
        for key, value in (await _analyze(session, target, name, data)).items():
            setattr(result, key, value)

    log.info("загрузка %s (%s, %d байт)", stored_as, content_type, len(data))
    return result


@uploads.get(
    "/files", response_model=FileListOut, summary="Что уже загружено", name="list_uploaded_files"
)
def list_files(
    request: Request,
    settings: Settings = Depends(get_settings),
    _key: None = Depends(require_upload_key),
) -> FileListOut:
    directory = settings.upload_path
    if not directory.is_dir():
        return FileListOut(files=[])
    found = [p for p in directory.iterdir() if p.is_file() and _STORED_NAME.match(p.name)]
    # Свежие сверху: шорткат смотрит в список сразу после отправки файла.
    found.sort(key=lambda p: (-p.stat().st_mtime, p.name))
    return FileListOut(
        files=[
            FileOut(
                name=p.name, size=p.stat().st_size, url=_public_url(settings, request, p.name)
            )
            for p in found
        ]
    )


@uploads.get(
    "/files/{filename}",
    summary="Содержимое файла",
    response_class=FileResponse,
    responses={200: {"content": {"application/octet-stream": {}}}, 404: {}},
)
def get_file(filename: str, settings: Settings = Depends(get_settings)) -> FileResponse:
    # Проверка первая: имя обязано быть нашим — hex и известное расширение.
    # «..», слеши, нулевой байт и абсолютный путь не проходят по построению.
    if not _STORED_NAME.match(filename):
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)

    directory = settings.upload_path.resolve()
    target = (directory / filename).resolve()
    # Проверка вторая, независимая от первой: итоговый путь обязан лежать
    # внутри каталога загрузок. Дублирование намеренное — ошибка здесь стоит
    # чтения чужих файлов, а симлинк или хитрая нормализация регулярке
    # не видны.
    if not target.is_relative_to(directory) or not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)

    return FileResponse(
        target,
        media_type=_TYPE_BY_EXT.get(target.suffix, "application/octet-stream"),
        headers={
            # Без nosniff браузер вправе решить, что «картинка» — это HTML, и
            # выполнить чужой скрипт на нашем домене со всеми правами домена.
            "X-Content-Type-Options": "nosniff",
            # Имя уже проверено регуляркой, поэтому в заголовок попадает
            # безопасная строка, а не то, что прислал клиент.
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, max-age=3600",
        },
    )
