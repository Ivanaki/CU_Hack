"""Сборка HTTP-приложения.

Отдаёт JSON для Mini App, служебные эндпоинты заливки и сам Mini App
статикой — чтобы на демонстрации не поднимать отдельный веб-сервер.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.game import game
from app.api.quick import quick
from app.api.routes import ingest, me, public
from app.api.uploads import secure_stored_files, uploads
from app.db import init_db

ROOT = Path(__file__).resolve().parent.parent.parent
MINIAPP_DIR = ROOT / "miniapp" / "dist"

DESCRIPTION = """
Учёт личных финансов, который считает не «сколько списалось», а сколько
человек потратил на самом деле.

**Две группы эндпоинтов.**

`/api/v1/me/*` — для Telegram Mini App. Аутентификация подписью Telegram:
заголовок `X-Telegram-Init-Data` со значением `Telegram.WebApp.initData`.

`/api/v1/ingest/*` — служебная заливка данных за пользователя. Заголовок
`X-API-Key`. Сюда внешний сервис шлёт скриншоты приложения банка: указывает
`tg_id` человека и прикладывает картинки. Пользователь заводится сам при
первом обращении.

`/upload`, `/files` — приём файлов с шортката iPhone: двойное касание по
задней панели делает скриншот или голосовую запись и отправляет файл сюда.
Адрес один на всех, поэтому человека опознаёт личный ключ в заголовке
`X-Upload-Token` — тогда картинка сразу разбирается в операции. Ключ
выдаёт бот (`/link`) и `GET /api/v1/me/upload-link`. Без ключа файл просто
сохраняется, как и написано в ТЗ команды.

Файл опознанного человека виден только ему: в общем списке `GET /files`
его нет, а `GET /files/{имя}` требует подписанную ссылку (её мы выдаём в
ответе на загрузку и в `/api/v1/me/imports`), личный ключ или подпись
Mini App. Анонимные загрузки остаются открытыми — чьи они, мы не знаем.
"""


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    init_db()
    # Файлы, залитые до разделения каталогов, лежат в общем — а среди них
    # чужие скриншоты банка, которые раздавались по публичной ссылке.
    # Чинить такое руками на задеплоенном стенде никто не станет, поэтому
    # передеплой чинит и данные, а не только код.
    secure_stored_files()

    app = FastAPI(
        title="Честный месяц",
        version="1.0.0",
        description=DESCRIPTION,
        docs_url="/docs",
        redoc_url=None,
    )
    # Mini App живёт на домене telegram и ходит сюда из браузера.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    app.include_router(public)
    app.include_router(me)
    app.include_router(ingest)
    # Обязательно до mount ниже: StaticFiles на "/" — перехватчик всего, и
    # маршруты, зарегистрированные после него, недостижимы.
    app.include_router(uploads)
    app.include_router(quick)
    app.include_router(game)

    if MINIAPP_DIR.exists():
        app.mount("/", StaticFiles(directory=MINIAPP_DIR, html=True), name="miniapp")
    else:
        @app.get("/", include_in_schema=False)
        def index() -> dict:
            return {
                "service": "Честный месяц",
                "docs": "/docs",
                "note": "Mini App ещё не собран в miniapp/dist",
            }

    return app


app = create_app()
