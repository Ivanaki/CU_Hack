"""Телеграм-бот. Оболочка над app.service — своей логики здесь нет.

Работает на long polling: для хакатона это значит ноль требований к домену
и сертификатам, бот поднимается одной командой где угодно.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message

from app.analytics import insights as insights_mod
from app.analytics import reports as reports_mod
from app.analytics.periods import Period, split_into_weeks, week, year
from app.api.uploads import store_image
from app.bot import keyboards as kb
from app.bot import texts
from app.config import get_settings
from app.db import init_db, session_scope
from app.ledger.invariants import run_all
from app.models import Operation, OperationSource, fmt_money
from app.service import (
    add_quick_expense,
    answer_question,
    average_actions_per_day,
    count_open_questions,
    get_or_create_user,
    ingest_screenshot,
    ingest_statement,
    issue_upload_token,
    latest_data_month,
    open_questions,
    report_for,
    rotate_upload_token,
    skip_question,
)

log = logging.getLogger(__name__)
dp = Dispatcher()

MAX_FILE_BYTES = 12 * 1024 * 1024


# --------------------------------------------------------------------------
# Вспомогательное: вся работа с БД уходит в поток, чтобы не блокировать цикл
# --------------------------------------------------------------------------


async def in_db(func, *args, **kwargs):
    def run():
        with session_scope() as session:
            return func(session, *args, **kwargs)

    return await asyncio.to_thread(run)


def _period_from_key(session, user_id: int, key: str) -> Period:
    today = date.today()
    if key == "today":
        from app.analytics.periods import day

        return day(today)
    if key == "week":
        return week(today)
    if key == "year":
        return year(today)
    # По умолчанию — месяц, за который есть данные: на демонстрации календарь
    # может показывать пустой месяц, а показать надо цифры.
    return latest_data_month(session, user_id)


def _user(session, message_or_query) -> object:
    src = message_or_query.from_user
    name = " ".join(x for x in (src.first_name, src.last_name) if x) or src.username or ""
    return get_or_create_user(session, src.id, name)


# --------------------------------------------------------------------------
# Групповые чаты: ни цифр, ни ключей
# --------------------------------------------------------------------------
#
# Эти два хендлера стоят первыми намеренно: aiogram проверяет их в порядке
# объявления, и всё, что ниже, до группового чата уже не доходит.
#
# Privacy-mode телеграма здесь не помогает: команда, адресованная боту
# (`/link@honest_month_bot`), доходит до него именно в такой форме. Без
# фильтра бот публиковал личный ключ загрузки прямо в общий чат, а кнопка
# «Выпустить новую» под этим сообщением работала для любого участника —
# посторонний нажимал и получал уже свой ключ, опубликованный там же.
# Это ровно тот сценарий, ради которого личная ссылка и делалась.


def in_group(message: Message) -> bool:
    return message.chat.type != ChatType.PRIVATE


def query_in_group(query: CallbackQuery) -> bool:
    return query.message is not None and query.message.chat.type != ChatType.PRIVATE


async def _private_link(bot: Bot, payload: str = "") -> str:
    """Ссылка на личный чат с ботом. Пусто — имя бота выяснить не удалось."""
    try:
        username = (await bot.me()).username
    except Exception:
        # Имя бота — украшение ответа, из-за него хендлер падать не должен.
        log.warning("не удалось узнать имя бота для ссылки в личный чат")
        return ""
    if not username:
        return ""
    return f"https://t.me/{username}" + (f"?start={payload}" if payload else "")


@dp.message(in_group)
async def on_group_message(message: Message) -> None:
    """В общем чате бот не показывает ни денег, ни ключа.

    Отвечаем только на команды: на остальное в группе бот молчит, иначе
    он будет вклиниваться в чужой разговор.
    """
    body = message.text or message.caption or ""
    if not body.startswith("/"):
        return
    await message.reply(
        texts.group_notice(await _private_link(message.bot, "link")),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@dp.callback_query(query_in_group)
async def on_group_callback(query: CallbackQuery) -> None:
    """Кнопка под сообщением в группе доступна всем — значит, не работает ни для кого."""
    await query.answer("Это работает только в личном чате со мной", show_alert=True)


# --------------------------------------------------------------------------
# Команды
# --------------------------------------------------------------------------


@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    await in_db(lambda s: _user(s, message) and None)
    # Человек пришёл из группы по ссылке t.me/бот?start=link: он уже
    # попросил ключ там, где мы отказались его показывать, и заставлять
    # его набирать команду второй раз — лишний шаг на ровном месте.
    if (command.args or "").strip() == "link":
        await cmd_upload_link(message)
        return
    await message.answer(texts.START, reply_markup=kb.main_menu())


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP)


async def _send_report(message: Message, key: str) -> None:
    def work(session):
        user = _user(session, message)
        period = _period_from_key(session, user.id, key)
        report = report_for(session, user.id, period)
        wallets = reports_mod.wallet_traces(session, user.id, period)
        return texts.report_message(report, wallets=wallets), period.label

    text, _ = await in_db(work)
    await message.answer(text, reply_markup=kb.report_keyboard(key))


@dp.message(Command("month"))
@dp.message(F.text == "Месяц")
async def cmd_month(message: Message) -> None:
    await _send_report(message, "month")


@dp.message(Command("week"))
@dp.message(F.text == "Неделя")
async def cmd_week(message: Message) -> None:
    await _send_report(message, "week")


@dp.message(Command("today"))
async def cmd_today(message: Message) -> None:
    await _send_report(message, "today")


@dp.message(Command("year"))
async def cmd_year(message: Message) -> None:
    await _send_report(message, "year")


@dp.message(Command("streak"))
async def cmd_streak(message: Message) -> None:
    def work(session):
        user = _user(session, message)
        return texts.streak_message(
            user.streak_days, user.best_streak, average_actions_per_day(session, user.id)
        )

    await message.answer(await in_db(work))


@dp.message(Command("checkup"))
async def cmd_checkup(message: Message) -> None:
    """Проверка сходимости прямо в продукте, а не только в тестах."""

    def work(session):
        user = _user(session, message)
        result = run_all(session, user.id)
        period = latest_data_month(session, user.id)
        total = reports_mod.spend_total(session, user.id, period)
        by_weeks = sum(
            reports_mod.spend_total(session, user.id, w) for w in split_into_weeks(period)
        )
        return texts.checkup_message(result.problems, by_weeks == total)

    await message.answer(await in_db(work))


@dp.message(Command("questions"))
@dp.message(F.text == "Спорные")
async def cmd_questions(message: Message) -> None:
    await _push_next_question(message.chat.id, message.bot, message.from_user.id)


def _upload_base() -> str:
    """Внешний адрес сервиса для ссылки в боте.

    У бота нет запроса, из которого можно было бы собрать адрес, поэтому
    только настройка. Пусто — честно говорим об этом вместо ссылки на
    localhost, по которой телефон никуда не попадёт.
    """
    return (get_settings().public_base_url or "").rstrip("/")


def _upload_link_text(session, message_or_query, *, fresh: bool) -> str:
    base = _upload_base()
    if not base:
        return (
            "Внешний адрес сервиса не настроен: без него ссылка будет вести "
            "в никуда. Задайте PUBLIC_BASE_URL в .env и перезапустите сервис."
        )
    user = _user(session, message_or_query)
    token = rotate_upload_token(session, user) if fresh else issue_upload_token(session, user)
    return texts.upload_link_message(
        url=f"{base}/upload",
        token=token,
        issued_at=user.upload_token_at,
        fresh=fresh,
    )


# Кириллическое имя команды aiogram разбирает, но клиент Telegram не делает
# её кликабельной и в меню команд её не принимает — поэтому основное имя
# латиницей, а /связь идёт синонимом тем же стеком декораторов.
@dp.message(Command("link"))
@dp.message(Command("связь"))
async def cmd_upload_link(message: Message) -> None:
    """Личная ссылка, по которой файлы с телефона попадают именно в его книгу."""
    text = await in_db(_upload_link_text, message, fresh=False)
    await message.answer(
        text,
        reply_markup=kb.upload_link_keyboard(),
        # Ключ в сообщении, а адрес — наш же домен: предпросмотр тут ничего
        # не добавляет, а лишний поход телеграма за страницей не нужен.
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        # Сообщение с ключом не пересылается и не сохраняется: «перешлю
        # другу свой адрес» — самый естественный способ его отдать.
        protect_content=True,
    )


@dp.callback_query(F.data == "upload:rotate")
async def on_upload_rotate(query: CallbackQuery) -> None:
    # Проверка своя, не только в общем фильтре: эта кнопка выпускает секрет
    # тому, кто её нажал, а не тому, кому сообщение адресовано. В общем
    # чате это значит «посторонний нажал из любопытства — и получил ключ,
    # опубликованный на весь чат».
    if query.message is None or query.message.chat.type != ChatType.PRIVATE:
        await query.answer("Ключ выпускается только в личном чате", show_alert=True)
        return
    text = await in_db(_upload_link_text, query, fresh=True)
    await query.answer("Старая ссылка отозвана")
    await query.message.answer(
        text,
        reply_markup=kb.upload_link_keyboard(),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        protect_content=True,
    )


@dp.message(Command("tips"))
@dp.message(F.text == "Советы")
async def cmd_tips(message: Message) -> None:
    def work(session):
        user = _user(session, message)
        period = latest_data_month(session, user.id)
        report = report_for(session, user.id, period)
        items = insights_mod.collect(session, user.id, report)
        return items

    items = await in_db(work)
    # Полировка формулировок ходит в сеть, поэтому вне транзакции БД.
    polished = await asyncio.to_thread(insights_mod.polish, items)
    await message.answer(
        texts.insights_message(polished, insights_mod.total_saving(polished))
    )


# --------------------------------------------------------------------------
# Источники данных
# --------------------------------------------------------------------------


@dp.message(F.document)
async def on_document(message: Message) -> None:
    document = message.document
    if document.file_size and document.file_size > MAX_FILE_BYTES:
        await message.answer("Файл великоват. Пришлите выписку до 12 МБ.")
        return
    name = (document.file_name or "").lower()
    if not name.endswith((".csv", ".txt", ".tsv")):
        await message.answer(
            "Жду CSV-выписку из банка. Если под рукой только приложение — "
            "пришлите скриншот списка операций, я прочитаю его."
        )
        return

    note = await message.answer("Читаю выписку…")
    buffer = await message.bot.download(document)
    data = buffer.read()

    def work(session):
        user = _user(session, message)
        outcome = ingest_statement(
            session, user, data, document.file_name or "statement.csv", channel="bot"
        )
        if not outcome.ok:
            return None, outcome
        return (
            texts.import_message(
                source=outcome.imported.batch.source,
                added=len(outcome.imported.new_rows),
                duplicates=outcome.imported.duplicates,
                superseded=outcome.imported.superseded,
                questions=outcome.questions,
                trace=outcome.trace,
            ),
            outcome,
        )

    text, outcome = await in_db(work)
    if not outcome.ok:
        await note.edit_text(
            f"Не смог разобрать файл: {outcome.error}\n\n"
            "Поддерживаю выписки Т-Банка, Сбербанка и Альфы в CSV."
        )
        return
    await note.edit_text(text)
    await _send_report(message, "month")


@dp.message(F.photo)
async def on_photo(message: Message) -> None:
    note = await message.answer("Смотрю скриншот…")
    photo = message.photo[-1]  # самое большое доступное разрешение
    buffer = await message.bot.download(photo)
    blob = buffer.read()

    def work(session):
        user = _user(session, message)
        # Картинку сохраняем тем же способом, что и /upload: без этого в
        # «Что я присылал» у всех, кто шлёт скриншоты боту (а это почти
        # все), карточка без файла — нажать и посмотреть, что именно ты
        # прислал, не на что. Запись на диск идёт здесь, потому что весь
        # work уже выполняется в отдельном потоке.
        stored_as, content_type = store_image(get_settings(), blob)
        outcome = ingest_screenshot(
            session, user, [blob], channel="bot",
            stored_as=stored_as, content_type=content_type,
        )
        if not outcome.ok:
            return None, outcome
        label = {"receipt": "Чек", "statement": "Скриншот"}.get(outcome.image_kind, "Картинка")
        return (
            texts.import_message(
                source=f"{label} · {outcome.imported.batch.source}",
                added=len(outcome.imported.new_rows),
                duplicates=outcome.imported.duplicates,
                superseded=outcome.imported.superseded,
                questions=outcome.questions,
                trace=outcome.trace,
            ),
            outcome,
        )

    text, outcome = await in_db(work)
    if not outcome.ok:
        await note.edit_text(
            "Не разглядел операции на картинке. Подойдёт скриншот списка "
            "операций из приложения банка или фотография чека — или пришлите "
            "CSV-выписку файлом."
        )
        return
    await note.edit_text(text)


@dp.message(F.voice | F.audio)
async def on_voice(message: Message) -> None:
    from app.asr.transcribe import transcribe  # noqa: PLC0415

    voice = message.voice or message.audio
    buffer = await message.bot.download(voice)
    blob = buffer.read()
    text = await asyncio.to_thread(transcribe, blob)
    if not text:
        await message.answer(
            "Распознавание голоса сейчас выключено. Напишите текстом: «кофе 300»."
        )
        return
    await _handle_quick_entry(message, text, source=OperationSource.VOICE, heard=text)


@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    await _handle_quick_entry(message, message.text, source=OperationSource.MANUAL)


async def _handle_quick_entry(
    message: Message, text: str, *, source: OperationSource, heard: str | None = None
) -> None:
    def work(session):
        user = _user(session, message)
        row, outcome = add_quick_expense(session, user, text, source=source, channel="bot")
        if row is None:
            return None, outcome
        return (
            texts.quick_entry_ack(row.description, -row.amount, row.category_hint),
            outcome,
        )

    ack, outcome = await in_db(work)
    if ack is None:
        await message.answer(
            "Не нашёл сумму. Напишите, например: «кофе 300» или «такси 450».\n"
            "А выписку или скриншот из банка просто пришлите файлом или картинкой."
        )
        return
    prefix = f"<i>{heard}</i>\n\n" if heard else ""
    await message.answer(prefix + ack)


# --------------------------------------------------------------------------
# Очередь вопросов: один тап на операцию
# --------------------------------------------------------------------------


async def _push_next_question(chat_id: int, bot: Bot, tg_id: int) -> None:
    def work(session):
        user = get_or_create_user(session, tg_id)
        items = open_questions(session, user.id, limit=1)
        left = count_open_questions(session, user.id)
        if not items:
            return None, None, 0
        item = items[0]
        return texts.question_message(item, left=left - 1), item, left

    text, item, _ = await in_db(work)
    if item is None:
        await bot.send_message(
            chat_id,
            "Спорных операций нет. Все цифры за период разобраны.",
        )
        return
    await bot.send_message(chat_id, text, reply_markup=kb.question_keyboard(item))


@dp.callback_query(F.data.startswith("ans:"))
async def on_answer(query: CallbackQuery) -> None:
    _, review_id, code = query.data.split(":", 2)

    def work(session):
        user = get_or_create_user(session, query.from_user.id)
        item = answer_question(session, user, int(review_id), code)
        if item is None:
            return None, 0, 0
        period = latest_data_month(session, user.id)
        report = report_for(session, user.id, period)
        return item, report.awareness.percent, user.streak_days

    item, awareness, streak = await in_db(work)
    if item is None:
        await query.answer("Этот вопрос уже закрыт")
        return

    chosen = next(
        (o["label"] for o in item.options if o["code"] == item.answer_code), item.answer_code
    )
    await query.answer("Записал")
    await query.message.edit_text(
        f"{query.message.html_text}\n\n<b>{chosen}</b>\n"
        f"<i>Осознанность: {awareness}% · серия {streak} дн.</i>",
        reply_markup=None,
    )
    await _push_next_question(query.message.chat.id, query.bot, query.from_user.id)


@dp.callback_query(F.data.startswith("skip:"))
async def on_skip(query: CallbackQuery) -> None:
    review_id = int(query.data.split(":", 1)[1])

    def work(session):
        user = get_or_create_user(session, query.from_user.id)
        skip_question(session, user, review_id)

    await in_db(work)
    await query.answer("Отложил")
    await query.message.edit_text(
        f"{query.message.html_text}\n\n<i>Отложено. Сумма останется неразобранной.</i>",
        reply_markup=None,
    )
    await _push_next_question(query.message.chat.id, query.bot, query.from_user.id)


@dp.callback_query(F.data.startswith("why:"))
async def on_why(query: CallbackQuery) -> None:
    """Объяснимость: почему система вообще спрашивает именно это."""
    review_id = int(query.data.split(":", 1)[1])

    def work(session):
        from app.models import ReviewItem  # noqa: PLC0415

        item = session.get(ReviewItem, review_id)
        if item is None:
            return "Вопрос не найден."
        op = item.operation
        if op is None:
            return "Уточняю смысл операции, чтобы не испортить статистику."
        return (
            f"{op.explanation}\n\n"
            f"Правило: <code>{op.rule}</code>, уверенность {op.confidence:.0%}."
        )

    await query.answer(await in_db(work), show_alert=True)


@dp.callback_query(F.data == "questions")
async def on_questions_button(query: CallbackQuery) -> None:
    await query.answer()
    await _push_next_question(query.message.chat.id, query.bot, query.from_user.id)


@dp.callback_query(F.data.startswith("weeks:"))
async def on_weeks(query: CallbackQuery) -> None:
    def work(session):
        user = get_or_create_user(session, query.from_user.id)
        period = latest_data_month(session, user.id)
        rows = [
            (chunk.label, reports_mod.spend_total(session, user.id, chunk))
            for chunk in split_into_weeks(period)
        ]
        total = reports_mod.spend_total(session, user.id, period)
        return texts.weeks_message(rows, total)

    await query.answer()
    await query.message.answer(await in_db(work))


@dp.callback_query(F.data.startswith("tips:"))
async def on_tips(query: CallbackQuery) -> None:
    def work(session):
        user = get_or_create_user(session, query.from_user.id)
        period = latest_data_month(session, user.id)
        report = report_for(session, user.id, period)
        return insights_mod.collect(session, user.id, report)

    await query.answer()
    items = await in_db(work)
    polished = await asyncio.to_thread(insights_mod.polish, items)
    await query.message.answer(
        texts.insights_message(polished, insights_mod.total_saving(polished))
    )


@dp.callback_query(F.data.startswith("ops:"))
async def on_operations(query: CallbackQuery) -> None:
    """Список операций периода с пометкой, что и почему не попало в расходы."""

    def work(session):
        from sqlalchemy import select  # noqa: PLC0415

        user = get_or_create_user(session, query.from_user.id)
        period = latest_data_month(session, user.id)
        ops = session.scalars(
            select(Operation)
            .where(
                Operation.user_id == user.id,
                Operation.occurred_at >= period.start,
                Operation.occurred_at < period.end,
            )
            .order_by(Operation.occurred_at.desc())
            .limit(40)
        ).all()
        lines = [f"<b>Операции · {period.label}</b>", ""]
        for op in ops:
            mark = "  " if op.spend_amount else "·"
            note = "" if op.spend_amount else f"  <i>{_kind_note(op.kind)}</i>"
            shown = op.spend_amount or op.amount
            lines.append(
                f"{mark} {op.occurred_at:%d.%m} {op.title[:28]} — {fmt_money(shown)}{note}"
            )
        return "\n".join(lines)

    await query.answer()
    await query.message.answer(await in_db(work))


def _kind_note(kind: str) -> str:
    return {
        "transfer": "перевод себе",
        "wallet_topup": "пополнение кошелька",
        "cash_withdrawal": "снятие наличных",
        "debt_lend": "выдан долг",
        "debt_repay_in": "возврат долга",
        "debt_repay_out": "погашен долг",
        "reimbursement_in": "доля друга",
        "unknown": "не разобрано",
        "income": "доход",
    }.get(str(kind), "не трата")


@dp.callback_query(F.data.startswith("report:"))
async def on_report_button(query: CallbackQuery) -> None:
    await query.answer()

    def work(session):
        user = get_or_create_user(session, query.from_user.id)
        period = latest_data_month(session, user.id)
        return texts.report_message(report_for(session, user.id, period))

    await query.message.answer(await in_db(work), reply_markup=kb.report_keyboard("month"))


# --------------------------------------------------------------------------
# Запуск
# --------------------------------------------------------------------------


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = get_settings()
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN не задан — заполните .env")

    init_db()
    bot = Bot(
        settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    from app.bot.scheduler import start_scheduler  # noqa: PLC0415

    scheduler = start_scheduler(bot)
    log.info("бот запущен, LLM=%s", settings.llm_provider)
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
