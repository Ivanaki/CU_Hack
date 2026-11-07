"""Напоминания. Это и есть механика регулярности, живущая в продукте.

Бот приходит сам и приносит конкретный вопрос или конкретную цифру. Он
никогда не пишет «не забудьте внести траты»: просить вести учёт — ровно то,
из-за чего люди бросают трекеры.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.analytics.periods import day, month, week
from app.bot import keyboards as kb
from app.bot import texts
from app.config import get_settings
from app.db import session_scope
from app.models import User, fmt_money
from app.service import count_open_questions, latest_data_month, report_for

log = logging.getLogger(__name__)


def _daily_payload(session, user: User) -> tuple[str, bool] | None:
    """Что сказать пользователю вечером. None — значит молчать.

    Молчать — полноценный вариант: уведомление без содержания обесценивает
    все следующие.
    """
    pending = count_open_questions(session, user.id)
    today = date.today()
    report = report_for(session, user.id, week(today))
    day_report = report_for(session, user.id, day(today))
    month_report = report_for(session, user.id, latest_data_month(session, user.id))

    # Цифру дня показываем только тогда, когда за день реально были траты.
    # «Сегодня осознанность 100%» на пустом дне — правда, которая вводит в
    # заблуждение: понимать было нечего.
    spent_today = bool(day_report.spend_total)
    head = (
        f"Сегодня потрачено <b>{fmt_money(day_report.spend_total)}</b>, "
        f"осознанность дня {day_report.awareness.percent}%."
        if spent_today
        else "Сегодня трат не было."
    )

    if pending:
        noun = texts.plural(pending, "операция", "операции", "операций")
        taps = texts.plural(pending, "тап", "тапа", "тапов")
        unclear = month_report.awareness.unclear_volume
        tail = (
            f"Но за месяц я не понимаю, куда отнести {fmt_money(unclear)} — "
            f"это {pending} {noun}."
            if unclear
            else f"Но за месяц осталось {pending} {noun} без ответа."
        )
        return (
            f"{head}\n\n"
            f"{tail} Осознанность месяца — <b>{month_report.awareness.percent}%</b>. "
            f"Каждый вопрос закрывается одним тапом: {pending} {taps} — и всё сходится.\n\n"
            f"Серия: {user.streak_days} дн. Прервётся, если оставить как есть.",
            True,
        )

    if spent_today:
        return (
            f"{head} Я понимаю каждый рубль.\n\n"
            f"Вопросов нет, делать ничего не нужно — серия засчитана сама.\n\n"
            f"Серия: {user.streak_days} дн.",
            False,
        )

    if report.spend_total and report.prev_spend_total:
        delta = report.spend_total - report.prev_spend_total
        if abs(delta) > report.prev_spend_total * 0.25:
            direction = "больше" if delta > 0 else "меньше"
            return (
                f"На этой неделе потрачено <b>{fmt_money(report.spend_total)}</b> — "
                f"на {fmt_money(abs(delta))} {direction}, чем на прошлой.\n\n"
                f"Осознанность: {report.awareness.percent}%",
                False,
            )
    return None


async def _send_digest(bot: Bot) -> None:
    hour_now = _current_hour()

    def collect():
        with session_scope() as session:
            users = session.scalars(
                select(User).where(User.digest_enabled, User.digest_hour == hour_now)
            ).all()
            out = []
            for user in users:
                payload = _daily_payload(session, user)
                if payload is not None:
                    out.append((user.tg_id, payload[0], payload[1]))
            return out

    for tg_id, text, has_questions in await asyncio.to_thread(collect):
        try:
            await bot.send_message(
                tg_id, text, reply_markup=kb.digest_keyboard(has_questions)
            )
        except Exception as exc:  # noqa: BLE001 — один заблокировавший не мешает остальным
            log.warning("не смог отправить сводку %s: %s", tg_id, exc)


async def _send_weekly(bot: Bot) -> None:
    def collect():
        with session_scope() as session:
            users = session.scalars(select(User).where(User.digest_enabled)).all()
            out = []
            for user in users:
                report = report_for(session, user.id, week(date.today() - timedelta(days=1)))
                if report.spend_total:
                    out.append((user.tg_id, texts.report_message(report, title="Итоги недели")))
            return out

    for tg_id, text in await asyncio.to_thread(collect):
        try:
            await bot.send_message(tg_id, text, reply_markup=kb.report_keyboard("week"))
        except Exception as exc:  # noqa: BLE001
            log.warning("не смог отправить недельную сводку %s: %s", tg_id, exc)


async def _send_monthly(bot: Bot) -> None:
    """Итоги месяца: главный момент продукта."""
    from app.analytics import insights as insights_mod  # noqa: PLC0415

    def collect():
        with session_scope() as session:
            users = session.scalars(select(User).where(User.digest_enabled)).all()
            previous = date.today().replace(day=1) - timedelta(days=1)
            period = month(previous.year, previous.month)
            out = []
            for user in users:
                report = report_for(session, user.id, period)
                if not report.spend_total:
                    continue
                items = insights_mod.collect(session, user.id, report)
                out.append((user.tg_id, texts.report_message(report), items))
            return out

    for tg_id, text, items in await asyncio.to_thread(collect):
        try:
            await bot.send_message(tg_id, text)
            polished = await asyncio.to_thread(insights_mod.polish, items)
            await bot.send_message(
                tg_id, texts.insights_message(polished, insights_mod.total_saving(polished))
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("не смог отправить итоги месяца %s: %s", tg_id, exc)


def _current_hour() -> int:
    from datetime import datetime

    return datetime.now().hour


def start_scheduler(bot: Bot) -> AsyncIOScheduler:
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.tz)

    # Ежедневно в начале каждого часа: рассылаем тем, у кого настроен этот час.
    scheduler.add_job(_send_digest, "cron", minute=0, args=[bot], id="digest")
    # Недельная сводка в понедельник утром: неделя уже закрыта.
    scheduler.add_job(_send_weekly, "cron", day_of_week="mon", hour=10, args=[bot], id="weekly")
    # Итоги месяца первого числа.
    scheduler.add_job(_send_monthly, "cron", day=1, hour=11, args=[bot], id="monthly")

    scheduler.start()
    log.info("планировщик запущен (%s)", settings.tz)
    return scheduler
