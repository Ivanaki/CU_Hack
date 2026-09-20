"""Заливает демонстрационный набор конкретному пользователю и пишет ему в бота.

Нужно, чтобы показать продукт живьём без ручного прохода по сценарию:
    python -m scripts.demo_dm 1058389184
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiogram import Bot  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.types import FSInputFile  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.analytics import insights as insights_mod  # noqa: E402
from app.analytics.periods import month, split_into_weeks  # noqa: E402
from app.analytics.reports import spend_total  # noqa: E402
from app.bot import keyboards as kb  # noqa: E402
from app.bot import texts  # noqa: E402
from app.classify.pipeline import reclassify  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.ingest.base import parse_statement  # noqa: E402
from app.ingest.service import import_rows  # noqa: E402
from app.models import OperationSource, RawTransaction  # noqa: E402
from app.service import get_or_create_user, open_questions, report_for  # noqa: E402

DEMO = ROOT / "data" / "demo"
FILES = (
    ("tbank_2026-07_09.csv", OperationSource.IMPORT),
    ("alfa_2026-09.csv", OperationSource.IMPORT),
    ("wildberries_2026-09.csv", OperationSource.IMPORT),
    ("cash_voice_2026-09.csv", OperationSource.VOICE),
)


def prepare(tg_id: int, name: str) -> dict:
    init_db()
    with session_scope() as session:
        user = get_or_create_user(session, tg_id, name)
        already = session.scalar(
            select(RawTransaction.id).where(RawTransaction.user_id == user.id).limit(1)
        )
        if already is None:
            for filename, origin in FILES:
                bank, rows = parse_statement((DEMO / filename).read_bytes(), filename=filename)
                import_rows(
                    session, user.id, rows, source=bank, filename=filename, origin=origin
                )
        reclassify(session, user.id)
        user.streak_days = max(user.streak_days, 1)

        period = month(2026, 9)
        report = report_for(session, user.id, period)
        weeks = [
            (chunk.label, spend_total(session, user.id, chunk))
            for chunk in split_into_weeks(period)
        ]
        questions = open_questions(session, user.id, limit=1)
        return {
            "report_text": texts.report_message(report, title="Сентябрь 2026 · демо-набор"),
            "weeks_text": texts.weeks_message(weeks, report.spend_total),
            "insights": insights_mod.collect(session, user.id, report),
            "question": questions[0] if questions else None,
            "question_text": (
                texts.question_message(questions[0], left=0) if questions else ""
            ),
            "keyboard": kb.question_keyboard(questions[0]) if questions else None,
        }


async def send(tg_id: int, payload: dict) -> None:
    settings = get_settings()
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await bot.send_message(
            tg_id,
            "<b>Честный месяц</b> — бэкенд поднят и работает.\n\n"
            "Залил тебе демонстрационный набор: 104 операции за июль–сентябрь, "
            "три банка, кошелёк маркетплейса и наличные. Все семь сценариев из "
            "кейса внутри.\n\n"
            "Ниже — то, что увидит пользователь.",
            reply_markup=kb.main_menu(),
        )
        await bot.send_message(
            tg_id, payload["report_text"], reply_markup=kb.report_keyboard("month")
        )
        await bot.send_message(tg_id, payload["weeks_text"])

        items = payload["insights"]
        await bot.send_message(
            tg_id, texts.insights_message(items, insights_mod.total_saving(items))
        )

        if payload["question"] is not None:
            await bot.send_message(
                tg_id,
                "А вот механика удержания — спорная операция, один тап:",
            )
            await bot.send_message(
                tg_id, payload["question_text"], reply_markup=payload["keyboard"]
            )

        shot = DEMO / "screenshot_tbank.png"
        if shot.exists():
            await bot.send_photo(
                tg_id,
                FSInputFile(shot),
                caption=(
                    "Так выглядит второй путь ввода: пересылаешь скрин из банка — "
                    "и всё. На этом скрине модель читает 8 операций из 8, без выдумок, "
                    "за 6 секунд.\n\n"
                    "Попробуй: пришли мне любой скриншот списка операций, "
                    "CSV-выписку файлом или просто напиши «кофе 300»."
                ),
            )
    finally:
        await bot.session.close()


def main() -> None:
    tg_id = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    if not tg_id:
        raise SystemExit("нужен telegram id: python -m scripts.demo_dm 123456")
    name = sys.argv[2] if len(sys.argv) > 2 else ""
    payload = prepare(tg_id, name)
    asyncio.run(send(tg_id, payload))
    print(f"отправлено в {tg_id}")


if __name__ == "__main__":
    main()
