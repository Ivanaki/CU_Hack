"""Приём файлов с шортката iPhone.

Двойное касание по задней панели телефона делает скриншот или голосовую
запись и отправляет файл сюда. Это вход для данных пользователя, а не
файлохранилище: если мы узнали, чей это файл, мы тут же пробуем разобрать
его в операции тем же движком, что и остальную заливку.

Адрес у всех телефонов один, поэтому «кто прислал» решает не URL, а секрет
в запросе — персональный `upload_token`. Раньше на этом месте стоял
`tg_id` из запроса, и это была дыра: telegram id не секрет, его видит
каждый, кому человек писал, и его можно перебрать — то есть залить
операции в чужую книгу и испортить чужую аналитику. Теперь `tg_id`
принимается только вместе со служебным ключом, то есть от нашего же
сервиса.

Три вещи, из-за которых модуль выглядит сложнее, чем «сохрани файл»:

* тело читается потоком и обрывается на превышении лимита — иначе любой
  желающий заставил бы сервер сначала принять гигабайт, а потом ответить 413;
* имя из запроса никогда не участвует в построении пути, а расширение берётся
  из типа, определённого по содержимому: присланное имя — это ввод, которому
  нельзя верить;
* отдача файла закрыта двумя независимыми проверками и запретом на
  интерпретацию содержимого браузером.

Опознанный файл — это чужой скриншот банка, а не публичная картинка,
поэтому он и лежит отдельно: анонимная загрузка «по ТЗ» остаётся в общем
каталоге и в общем списке, а файл с хозяином уезжает в закрытый подкаталог
и отдаётся только ему — по личному ключу, по подписи Mini App или по
короткоживущей подписи в самой ссылке (без неё превью в Mini App
не показать: тег img заголовков не шлёт).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.api.auth import db_session, same_secret, verify_init_data
from app.config import Settings, get_settings
from app.db import session_scope
from app.models import OperationSource, User
from app.service import (
    add_quick_expense,
    get_or_create_user,
    ingest_screenshot,
    owned_stored_names,
    owner_of_file,
    remember_upload,
    user_by_upload_token,
)

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
# Подкаталог для файлов, у которых есть хозяин. Каталог, а не колонка в
# базе: по имени файла на диске сразу видно, публичный он или личный, и
# ошибка в SQL не может внезапно открыть чужой скриншот всему интернету.
PRIVATE_DIR = "owned"
_PRIVATE_DENIED = "файл личный: нужна ваша ссылка или подпись Mini App"
# Один и тот же ответ на неизвестный, чужой и отозванный токен: по нему
# нельзя понять, существует такой секрет или нет.
_BAD_TOKEN = "ссылка не опознана"

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

    # Необязательная часть: появляется, только если мы узнали хозяина файла
    # и файл — картинка. Клиент, написанный строго по ТЗ, этих полей не
    # увидит: ответ отдаётся с response_model_exclude_none.
    ingested: bool | None = Field(default=None, description="Удалось ли прочитать операции")
    operations: int | None = Field(
        default=None, description="Операций у пользователя после разбора"
    )
    questions: int | None = Field(default=None, description="Открытых вопросов после разбора")
    # Что услышали в голосовой записке. Человек обязан видеть распознанный
    # текст: без него непонятно, почему записалось не то, что он сказал.
    heard: str | None = Field(default=None, description="Распознанный текст записки")

    # Что распозналось именно в этом файле. Прошлые два поля описывают всю
    # книгу после пересчёта, а человек спрашивает про то, что скинул сейчас.
    batch_id: int | None = Field(
        default=None, description="Номер загрузки: по нему видно, что из файла получилось"
    )
    added: int | None = Field(default=None, description="Операций добавилось из этого файла")
    duplicates: int | None = Field(default=None, description="Строк отброшено как дубли")
    new_questions: int | None = Field(
        default=None, description="Вопросов появилось по этому файлу"
    )
    # Повторная присылка того же скриншота — самый частый случай, и при
    # ней added=0. Без этого числа ответ читается как «ничего не вышло»,
    # хотя на картинке восемь уже известных операций.
    recognized: int | None = Field(
        default=None, description="Операций видно по этому файлу, включая уже известные"
    )

    # Не про содержимое файла, а про то, как он приехал: единственный
    # способ сказать «ключ в адресе — пересоберите команду» тому, кто
    # журнал сервера никогда не увидит.
    warning: str | None = Field(default=None, description="Замечание к самому запросу")


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


def public_base(settings: Settings, request: Request) -> str:
    """Внешний адрес сервиса без завершающего слеша.

    Базовый адрес берём из настройки: снаружи сервис стоит за туннелем и
    приходит по https, а внутрь запрос попадает по http. Настройки нет —
    собираем адрес из заголовков прокси, см. `_base_from_request`.
    """
    base = settings.public_base_url or _base_from_request(request)
    return base.rstrip("/")


# --------------------------------------------------------------------------
# Подписанная ссылка на личный файл
# --------------------------------------------------------------------------
#
# Заголовок в теге <img> не пошлёшь, а превью своего же скриншота в Mini App
# показать надо. Поэтому у ссылки на личный файл есть подпись: она говорит
# «эту ссылку выдали владельцу», живёт несколько часов и не даёт ничего,
# кроме одного конкретного файла.

FILE_LINK_TTL = 6 * 3600
# Срок округляется вверх до часа, чтобы одна и та же ссылка на один и тот же
# файл не менялась при каждом запросе: иначе браузер перекачивает картинку
# заново, а два ответа API про один файл выглядят как два разных файла.
_FILE_LINK_STEP = 3600
# Запасной секрет на случай, когда не задано вообще ничего (локальный
# запуск без .env). Свой на процесс: ссылки переживают запрос, но не
# перезапуск — это честнее, чем подписывать пустой строкой.
_FALLBACK_SECRET = secrets.token_urlsafe(32)


def _link_secret(settings: Settings) -> str:
    return (
        settings.bot_token
        or settings.ingest_api_key
        or settings.upload_api_key
        or _FALLBACK_SECRET
    )


def _sign_name(name: str, expires: int, settings: Settings) -> str:
    body = f"{name}.{expires}".encode()
    return hmac.new(_link_secret(settings).encode(), body, hashlib.sha256).hexdigest()[:24]


def file_signature(name: str, settings: Settings, *, ttl: int = FILE_LINK_TTL) -> str:
    expires = ((int(time.time()) + ttl) // _FILE_LINK_STEP + 1) * _FILE_LINK_STEP
    return f"{expires}.{_sign_name(name, expires, settings)}"


def _signature_ok(name: str, raw: str, settings: Settings) -> bool:
    expires, _, digest = raw.partition(".")
    if not expires.isdigit() or not digest:
        return False
    if int(expires) < time.time():
        return False
    return same_secret(digest, _sign_name(name, int(expires), settings))


def file_url(settings: Settings, request: Request, name: str, *, signed: bool = False) -> str:
    """Ссылка на файл. signed — ссылка для хозяина, она открывается сама."""
    base = f"{public_base(settings, request)}/files/{name}"
    return f"{base}?s={file_signature(name, settings)}" if signed else base


# --------------------------------------------------------------------------
# Каталоги: публичный и закрытый
# --------------------------------------------------------------------------


def private_dir(settings: Settings) -> Path:
    return settings.upload_path / PRIVATE_DIR


def _store(directory: Path, data: bytes, ext: str) -> str:
    """Кладёт файл под своим именем и возвращает его."""
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    tmp = directory / f".{name}.part"
    tmp.write_bytes(data)
    # Переименование атомарно: в списке файлов не может появиться половина.
    os.replace(tmp, directory / name)
    return name


def store_image(settings: Settings, data: bytes) -> tuple[str, str]:
    """Кладёт картинку человека в закрытый каталог: имя на диске и тип.

    Общая точка для всех каналов: шорткат, бот и Mini App обязаны
    сохранять картинку одинаково, иначе «что именно я присылал» работает
    в одном канале из трёх, а в остальных карточка пустая.

    Пустой ответ — не ошибка: файл не сохранился, но операции из него всё
    равно разберутся. Терять данные из-за диска мы не станем.
    """
    mime = sniff_mime(data[:512]) or ""
    if not mime.startswith("image/") or mime not in ALLOWED_TYPES:
        return "", ""
    try:
        return _store(private_dir(settings), data, ALLOWED_TYPES[mime]), mime
    except OSError:
        log.exception("картинку не удалось сохранить, разбор продолжаем")
        return "", ""


def secure_stored_files(settings: Settings | None = None) -> int:
    """Переносит уже лежащие файлы с хозяином в закрытый каталог.

    Файлы, залитые до разделения каталогов, остались в общем — а среди них
    чужие скриншоты банка, которые раздавались по публичной ссылке. Разовый
    проход при старте: сам факт передеплоя чинит и данные, а не только код.
    """
    settings = settings or get_settings()
    public = settings.upload_path
    if not public.is_dir():
        return 0
    with session_scope() as session:
        owned = owned_stored_names(session)
    moved = 0
    for name in owned:
        source = public / name
        if not source.is_file() or not _STORED_NAME.match(name):
            continue
        target = private_dir(settings) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        moved += 1
    if moved:
        log.info("закрыто от публичного списка файлов: %d", moved)
    return moved


async def _analyze(
    session: Session, user_id: int, data: bytes, *, stored_as: str, content_type: str
) -> dict[str, Any]:
    """Прогоняет картинку через обычный разбор скриншотов.

    Ошибка разбора не должна ломать загрузку: файл уже на диске, и клиент
    ждёт ссылку, а не отчёт о том, что модель не ответила.

    Через границу треда идёт голый user_id, а не ORM-объект: сессия одна на
    запрос, и таскать привязанные к ней объекты между потоками — способ
    получить неповторяемую ошибку в самый неудобный момент.
    """

    def run() -> Any:
        user = session.get(User, user_id)
        # SAVEPOINT: если разбор упадёт, откатится только разбор. Общий
        # откат снёс бы заодно запись о том, чей это файл, — и файл в
        # закрытом каталоге стал бы ничьим, то есть недоступным даже
        # хозяину, которому мы только что выдали на него ссылку.
        with session.begin_nested():
            return ingest_screenshot(
                session, user, [data],
                channel="shortcut", stored_as=stored_as, content_type=content_type,
            )

    try:
        # В этом же процессе живёт бот, а разбор ходит в сеть и считает
        # полный пересчёт. В корутине он остановил бы и бота, и API.
        outcome = await asyncio.to_thread(run)
    except Exception:
        log.exception("разбор картинки с шортката не удался")
        return {"ingested": False, "operations": None, "questions": None}

    found: dict[str, Any] = {
        "ingested": bool(outcome.ok),
        "operations": outcome.operations,
        "questions": outcome.questions,
    }
    return _with_trace(found, outcome)


async def _listen(
    session: Session,
    user_id: int,
    data: bytes,
    *,
    stored_as: str,
    content_type: str,
    filename: str,
) -> dict[str, Any]:
    """Голосовая записка с телефона: распознать и записать трату.

    Быстрая команда по двойному касанию умеет отправлять и звук, и без
    этого разбора записка просто ложилась файлом на диск: человек сказал
    «кофе триста», получил «файл принят» — и ничего не произошло. Для него
    это неотличимо от поломки.
    """
    from app.asr.transcribe import transcribe  # noqa: PLC0415

    def run() -> tuple[str, Any]:
        heard = transcribe(data)
        if not heard:
            return "", None
        user = session.get(User, user_id)
        # SAVEPOINT по той же причине, что и у картинки: падение разбора не
        # должно уносить с собой запись о том, чей это файл.
        with session.begin_nested():
            _row, outcome = add_quick_expense(
                session, user, heard,
                source=OperationSource.VOICE, channel="shortcut",
                stored_as=stored_as, content_type=content_type,
                size_bytes=len(data), filename=filename,
            )
        return heard, outcome

    try:
        heard, outcome = await asyncio.to_thread(run)
    except Exception:
        log.exception("разбор голосовой записки с шортката не удался")
        return {"ingested": False, "operations": None, "questions": None}

    if not heard:
        # Честнее сказать, что не расслышали, чем промолчать: человек
        # решит, что трата записана, и не проверит.
        return {"ingested": False, "heard": "", "operations": None, "questions": None}
    if outcome is None or not outcome.ok:
        return {"ingested": False, "heard": heard, "operations": None, "questions": None}

    found: dict[str, Any] = {
        "ingested": True,
        "heard": heard,
        "operations": outcome.operations,
        "questions": outcome.questions,
    }
    return _with_trace(found, outcome)


def _with_trace(found: dict[str, Any], outcome: Any) -> dict[str, Any]:
    """Добавляет к ответу след загрузки: что дал именно этот файл."""
    trace = outcome.trace
    if trace is not None:
        # Номер загрузки — то, по чему потом поднимается весь след:
        # GET /api/v1/me/imports/{id} показывает и операции, и вопросы.
        found["batch_id"] = trace.batch_id
        found["added"] = trace.operations
        found["duplicates"] = trace.duplicates
        found["new_questions"] = trace.questions
        # Тот же скриншот второй раз: added=0 — правда, но не вся. Человек
        # видит на картинке восемь своих операций, и ответ обязан их
        # признать, иначе повторная присылка читается как «не сработало».
        found["recognized"] = trace.recognized
    return found


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
    if not same_secret(x_api_key, settings.upload_api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "неверный X-API-Key")


def _is_service_call(settings: Settings, x_api_key: str) -> bool:
    """Пришёл ли запрос от нашего же сервиса.

    Проверка, а не зависимость: анонимная загрузка без ключа обязана
    работать по ТЗ шортката, а зависимость ответила бы ей 401.

    Сверяем с ingest_api_key — тем же секретом, что у /api/v1/ingest/*:
    это и есть «ключ, по которому можно писать данные любому
    пользователю». Брать для этого upload_api_key нельзя: он отвечает на
    другой вопрос («пускать ли вообще на приём») и при пустом значении
    пропускает всех — то есть ровно в задеплоенной конфигурации подмена
    tg_id снова стала бы возможной.
    """
    if not settings.ingest_api_key:
        return False
    return same_secret(x_api_key, settings.ingest_api_key)


# Что видно в логе от отклонённого ключа: столько, чтобы отличить свою
# старую команду от перебора, и не столько, чтобы ключ можно было собрать.
_TOKEN_HINT = 6


def client_ip(request: Request) -> str:
    """Кто постучался. За туннелем request.client.host всегда 127.0.0.1."""
    headers = request.headers
    forwarded = (headers.get("x-forwarded-for") or "").split(",")[0].strip()
    direct = request.client.host if request.client else ""
    return headers.get("cf-connecting-ip") or forwarded or direct or "?"


@dataclass(slots=True)
class _Token:
    """Секрет загрузки и то, как он приехал."""

    value: str = ""
    # Ключ в строке запроса — рабочий, но плохой путь: адрес целиком
    # попадает в журналы прокси и пересылается вместе со «ссылкой».
    from_url: bool = False


def _token_from(
    fields: dict[str, str], query: str, header: str, authorization: str
) -> _Token:
    """Секрет загрузки из запроса. Заголовок важнее строки запроса.

    В URL секрету не место: полный адрес со строкой запроса пишут все —
    uvicorn, nginx, cloudflared, — а ссылку целиком человек может
    переслать в чат. Но «Быстрые команды» одинаково умеют и заголовок, и
    URL, поэтому принимаем оба: путь из ТЗ остаётся рабочим, а человек
    получает в ответе и в логе прямое «пересоберите команду».
    """
    scheme, _, value = authorization.partition(" ")
    bearer = value.strip() if scheme.lower() == "bearer" else ""
    known = header.strip() or bearer or fields.get("token", "").strip()
    if known:
        return _Token(known)
    return _Token(query.strip(), from_url=bool(query.strip()))


@dataclass(slots=True)
class _Identity:
    """Кого опознали и о чём надо сказать вслух после сохранения файла."""

    user: User | None = None
    # tg_id, который прислали без служебного ключа. Нужен в логе целиком:
    # он не секрет, и без него запись «кто-то пробовал» бесполезна —
    # ни своего с устаревшей командой не позвать, ни перебор не увидеть.
    ignored_tg_id: int | None = None


def _identify(
    session: Session,
    request: Request,
    *,
    token: _Token,
    tg_id: int | None,
    display_name: str,
    service: bool,
) -> _Identity:
    """Чей это файл. Пустой user — аноним, файл просто сохраняется.

    Токен — основной путь: у каждого своя ссылка, один раз вшитая в его
    быструю команду. tg_id остаётся только для служебной заливки: он не
    секрет, и по нему кто угодно залил бы операции в чужую книгу.
    """
    if token.value:
        user = user_by_upload_token(session, token.value)
        if user is None:
            # Неизвестный, чужой и отозванный секрет отвечают одинаково,
            # чтобы по ответу нельзя было узнать, что такой существует.
            # В лог идёт начало ключа и адрес: по ним видно, один это
            # человек с устаревшей командой или кто-то перебирает.
            log.warning(
                "загрузка с непризнанной ссылкой отклонена: ключ %s…, адрес %s",
                token.value[:_TOKEN_HINT], client_ip(request),
            )
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, _BAD_TOKEN)
        return _Identity(user=user)
    if tg_id is None:
        return _Identity()
    if not service:
        # Молча для клиента, но с именем и адресом в логе: это либо старая
        # команда, которую пора пересобрать, либо чужое любопытство, и
        # различить их можно только по этой записи.
        return _Identity(ignored_tg_id=tg_id)
    return _Identity(user=get_or_create_user(session, tg_id, display_name))


def _may_read_private(
    session: Session,
    settings: Settings,
    *,
    filename: str,
    signature: str,
    token: str,
    init_data: str,
    debug_tg_id: str = "",
) -> bool:
    """Хозяин ли смотрит на личный файл. Три равноправных доказательства.

    Хозяин находится по записи о загрузке: `stored_as` — единственная
    ниточка от файла на диске к человеку. Файл, за которым такой записи
    нет, не отдаётся никому: мы не знаем, чей он, а лежит он в закрытом
    каталоге — значит, приехал не анонимно.
    """
    owner_id = owner_of_file(session, filename)
    if owner_id is None:
        return False
    if signature and _signature_ok(filename, signature, settings):
        return True
    if token:
        user = user_by_upload_token(session, token)
        if user is not None and user.id == owner_id:
            return True
    if init_data and settings.bot_token:
        try:
            payload = verify_init_data(init_data, settings.bot_token)
        except HTTPException:
            return False
        return _is_owner(session, owner_id, payload.get("id"))
    # Отладочный заголовок работает ровно там же, где он работает в
    # остальном Mini App: когда подписывать initData нечем, потому что
    # токена бота нет. В рабочей конфигурации этот путь закрыт.
    if debug_tg_id.isdigit() and not settings.bot_token:
        return _is_owner(session, owner_id, int(debug_tg_id))
    return False


def _is_owner(session: Session, owner_id: int, tg_id: object) -> bool:
    owner = session.get(User, owner_id)
    return owner is not None and tg_id is not None and int(tg_id) == owner.tg_id


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
    token: str = Query(default="", description="Персональная ссылка: чей это файл"),
    tg_id: int | None = Query(
        default=None, description="Кому засчитать картинку; только со служебным ключом"
    ),
    display_name: str = Query(default=""),
    analyze: bool = Query(default=True, description="Разбирать картинку в операции"),
    x_filename: str = Header(default="", alias="X-Filename"),
    x_tg_id: str = Header(default="", alias="X-Tg-Id"),
    x_upload_token: str = Header(default="", alias="X-Upload-Token"),
    authorization: str = Header(default="", alias="Authorization"),
    x_api_key: str = Header(default="", alias="X-API-Key"),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(db_session),
    _key: None = Depends(require_upload_key),
) -> UploadOut:
    """Два способа прислать файл: форма с полем «file» или файл прямо в теле.

    Второй способ — ради шортката: он умеет POST-запрос с файлом в теле и
    заголовками, но не умеет собирать multipart.

    Кто прислал — решает `X-Upload-Token` (он же `Authorization: Bearer`,
    поле формы `token` или `?token=`). Без него файл просто сохраняется и
    возвращается ссылка, как и написано в ТЗ команды.
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

    # Опознаём до записи на диск: файл по непризнанной ссылке не должен
    # оставлять следов. Это один индексный SELECT — в отличие от разбора,
    # который ходит в сеть и потому уезжает в отдельный тред.
    key = _token_from(fields, token, x_upload_token, authorization)
    identity = _identify(
        session,
        request,
        token=key,
        tg_id=_as_tg_id(fields.get("tg_id")) or tg_id or _as_tg_id(x_tg_id),
        display_name=_clean_name(fields.get("display_name", "")) or display_name,
        service=_is_service_call(settings, x_api_key),
    )
    owner = identity.user

    # Файл с хозяином — чужой скриншот банка, и в общем каталоге ему не
    # место: оттуда его забирает кто угодно, зная только публичный адрес.
    directory = private_dir(settings) if owner is not None else settings.upload_path
    stored_as = _store(directory, data, ALLOWED_TYPES[content_type])
    shown_name = _clean_name(part_name or filename or x_filename) or stored_as

    result = UploadOut(
        filename=shown_name,
        stored_as=stored_as,
        content_type=content_type,
        size=len(data),
        # Ссылка хозяина подписана: иначе его собственный файл не открылся
        # бы ни в браузере, ни в Mini App — заголовок туда не подставить.
        url=file_url(settings, request, stored_as, signed=owner is not None),
    )
    if key.from_url:
        result.warning = (
            "ключ приехал в адресе: его видят журналы прокси и он уезжает "
            "вместе с пересланной ссылкой. Пересоберите команду на заголовок "
            "X-Upload-Token"
        )

    wanted = analyze and _as_bool(fields.get("analyze"), True)
    analyzed: dict[str, Any] | None = None
    if owner is not None and wanted and content_type.startswith("image/"):
        analyzed = await _analyze(
            session, owner.id, data, stored_as=stored_as, content_type=content_type
        )
    elif owner is not None and wanted and content_type.startswith("audio/"):
        # Голос — второй вход быстрой команды по двойному касанию, наравне
        # со скриншотом. Без него записка молча ложилась на диск.
        analyzed = await _listen(
            session, owner.id, data,
            stored_as=stored_as, content_type=content_type, filename=shown_name,
        )
    if analyzed is not None:
        for name, value in analyzed.items():
            setattr(result, name, value)

    # Разбор мог не состояться: не картинка, analyze=false, модель не
    # ответила. Файл при этом уже в закрытом каталоге, а тот отдаёт только
    # хозяину — и хозяин находится единственным способом, через запись о
    # загрузке. Без неё мы бы вернули человеку подписанную ссылку на его
    # собственный файл, которая отвечает 401.
    if owner is not None and owner_of_file(session, stored_as) is None:
        remember_upload(
            session, owner.id,
            stored_as=stored_as, content_type=content_type,
            size_bytes=len(data), channel="shortcut", filename=shown_name,
        )

    # В логе — кто и что, но никогда сам токен: строчку с ним увидел бы и
    # cloudflared, и всякий, кому мы покажем журнал.
    log.info(
        "загрузка %s (%s, %d байт) от %s",
        stored_as, content_type, len(data),
        f"пользователя {owner.id}" if owner is not None else "анонима",
    )
    if identity.ignored_tg_id is not None:
        log.warning(
            "tg_id %s в загрузке без служебного ключа — игнорирую, "
            "файл %s сохранён анонимно, адрес %s",
            identity.ignored_tg_id, stored_as, client_ip(request),
        )
    if key.from_url:
        log.warning(
            "ключ загрузки приехал в адресе (файл %s, адрес %s) — "
            "команду пора пересобрать на заголовок",
            stored_as, client_ip(request),
        )
    return result


@uploads.get(
    "/files", response_model=FileListOut, summary="Что уже загружено", name="list_uploaded_files"
)
def list_files(
    request: Request,
    settings: Settings = Depends(get_settings),
    _key: None = Depends(require_upload_key),
) -> FileListOut:
    """Только анонимные файлы «по ТЗ».

    Личные сюда не попадают по построению: они лежат в подкаталоге, а
    обход каталога не рекурсивный. Это важнее, чем кажется: раньше список
    сам называл случайные имена чужих скриншотов, и «неугадываемое имя»
    переставало быть защитой.
    """
    directory = settings.upload_path
    if not directory.is_dir():
        return FileListOut(files=[])
    found = [p for p in directory.iterdir() if p.is_file() and _STORED_NAME.match(p.name)]
    # Свежие сверху: шорткат смотрит в список сразу после отправки файла.
    found.sort(key=lambda p: (-p.stat().st_mtime, p.name))
    return FileListOut(
        files=[
            FileOut(
                name=p.name, size=p.stat().st_size, url=file_url(settings, request, p.name)
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
def get_file(
    filename: str,
    s: str = Query(default="", description="Подпись ссылки, выданной хозяину файла"),
    x_upload_token: str = Header(default="", alias="X-Upload-Token"),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
    x_debug_tg_id: str = Header(default="", alias="X-Debug-Tg-Id"),
    session: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """Содержимое файла.

    Анонимный файл открыт — так написано в ТЗ команды, и ничьих личных
    данных в нём по определению нет: мы не знаем, чей он. Файл с хозяином
    отдаётся только хозяину, тремя равноправными способами: подписанной
    ссылкой, личным ключом загрузки или подписью Mini App.
    """
    # Проверка первая: имя обязано быть нашим — hex и известное расширение.
    # «..», слеши, нулевой байт и абсолютный путь не проходят по построению.
    if not _STORED_NAME.match(filename):
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)

    directory = settings.upload_path.resolve()
    private = (private_dir(settings) / filename).resolve()
    target = private if private.is_file() else (directory / filename).resolve()
    # Проверка вторая, независимая от первой: итоговый путь обязан лежать
    # внутри каталога загрузок. Дублирование намеренное — ошибка здесь стоит
    # чтения чужих файлов, а симлинк или хитрая нормализация регулярке
    # не видны.
    if not target.is_relative_to(directory) or not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)

    if target == private and not _may_read_private(
        session, settings,
        filename=filename, signature=s, token=x_upload_token,
        init_data=x_telegram_init_data, debug_tg_id=x_debug_tg_id,
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _PRIVATE_DENIED)

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
