"""Аутентификация двух видов.

Mini App приходит от имени пользователя и подтверждает это подписью
Telegram. Служебный API приходит от нашего же сервиса и подтверждает это
ключом. Смешивать их нельзя: по служебному ключу можно писать данные
любому пользователю, и он никогда не должен попасть в браузер.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import session_scope
from app.models import User
from app.service import get_or_create_user

INIT_DATA_TTL = 24 * 3600


def same_secret(given: str, expected: str) -> bool:
    """Сравнение секретов: постоянное по времени и без сюрпризов на кириллице.

    `hmac.compare_digest` на обычных строках бросает TypeError, если в них
    есть хоть один не-ASCII символ. В `.env.example` ключ-плейсхолдер был
    как раз русским, и любой, кто развернул проект по инструкции и не
    заменил его, получал 500 на ручке из ТЗ вместо отказа. Байты сравнивать
    можно любые, поэтому сравниваем их.
    """
    if not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def db_session():
    with session_scope() as session:
        yield session


def verify_init_data(init_data: str, bot_token: str, *, ttl: int = INIT_DATA_TTL) -> dict:
    """Проверяет подпись Telegram WebApp initData.

    Без этой проверки любой мог бы подставить чужой user_id в запросе и
    читать чужие финансы. Алгоритм — из документации Telegram: ключ
    выводится из токена бота, подпись считается по отсортированным парям.
    """
    if not init_data:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "нет initData")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "в initData нет подписи")

    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    # same_secret, а не compare_digest напрямую: `hash` приходит из
    # запроса, и одна кириллическая буква в нём превращала бы отказ в 500.
    if not same_secret(received, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "подпись initData не сходится")

    issued = int(pairs.get("auth_date") or 0)
    if ttl and issued and time.time() - issued > ttl:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "initData просрочена")

    try:
        return json.loads(pairs.get("user") or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "испорченный user в initData") from exc


def current_user(
    session: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
    x_debug_tg_id: str = Header(default="", alias="X-Debug-Tg-Id"),
) -> User:
    """Пользователь Mini App.

    X-Debug-Tg-Id работает только когда не задан токен бота — то есть в
    локальной разработке, где подписывать initData нечем. В рабочей
    конфигурации этот путь закрыт.
    """
    if not settings.bot_token:
        if not x_debug_tg_id.isdigit():
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "нет ни токена бота, ни отладочного id"
            )
        return get_or_create_user(session, int(x_debug_tg_id), "Отладка")

    payload = verify_init_data(x_telegram_init_data, settings.bot_token)
    tg_id = payload.get("id")
    if not tg_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "в initData нет пользователя")
    name = " ".join(
        x for x in (payload.get("first_name"), payload.get("last_name")) if x
    ) or payload.get("username", "")
    return get_or_create_user(session, int(tg_id), name)


def require_service_key(
    settings: Settings = Depends(get_settings),
    x_api_key: str = Header(default="", alias="X-API-Key"),
) -> None:
    if not settings.ingest_api_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "служебный API выключен: не задан INGEST_API_KEY"
        )
    if not same_secret(x_api_key, settings.ingest_api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "неверный X-API-Key")
