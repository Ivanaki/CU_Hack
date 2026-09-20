"""Единая точка запуска: бот и HTTP API в одном процессе.

Один процесс — потому что на хакатоне разворачивать два сервиса и следить
за ними дороже, чем польза от разделения. Расцепить их можно в любой
момент: общее состояние только в базе.

    python -m app.main          # бот + API
    python -m app.main --api    # только API
    python -m app.main --bot    # только бот
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging

import uvicorn

from app.config import get_settings
from app.db import init_db

log = logging.getLogger(__name__)


async def serve_api() -> None:
    settings = get_settings()
    config = uvicorn.Config(
        "app.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
        access_log=False,
    )
    await uvicorn.Server(config).serve()


async def serve_bot() -> None:
    from app.bot.main import run  # noqa: PLC0415

    await run()


async def serve_both() -> None:
    api = asyncio.create_task(serve_api(), name="api")
    bot = asyncio.create_task(serve_bot(), name="bot")
    done, pending = await asyncio.wait({api, bot}, return_when=asyncio.FIRST_EXCEPTION)
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    for task in done:
        task.result()  # пробрасываем исключение, если оно было


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description="Честный месяц")
    parser.add_argument("--api", action="store_true", help="только HTTP API")
    parser.add_argument("--bot", action="store_true", help="только телеграм-бот")
    args = parser.parse_args()

    init_db()
    settings = get_settings()
    log.info(
        "старт: LLM=%s (%s), API=%s:%s, заливка по ключу=%s",
        settings.llm_provider,
        settings.llm_model_vision,
        settings.api_host,
        settings.api_port,
        "включена" if settings.ingest_api_key else "выключена",
    )

    if args.api:
        asyncio.run(serve_api())
    elif args.bot:
        asyncio.run(serve_bot())
    else:
        asyncio.run(serve_both())


if __name__ == "__main__":
    main()
