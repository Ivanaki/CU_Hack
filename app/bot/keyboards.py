"""Клавиатуры. Главное правило: ответ на вопрос — один тап, без подменю."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from app.config import get_settings
from app.models import ReviewItem


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Месяц"), KeyboardButton(text="Неделя")],
            [KeyboardButton(text="Спорные"), KeyboardButton(text="Советы")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выписка, скриншот или «кофе 300»",
    )


def question_keyboard(item: ReviewItem) -> InlineKeyboardMarkup:
    """Кнопки ответа. Раскладка зависит от количества вариантов, но тап всегда один."""
    options = item.options or []
    buttons: list[list[InlineKeyboardButton]] = []
    longest = max((len(o["label"]) for o in options), default=0)
    per_row = 2 if len(options) > 3 or longest > 14 else 1
    if all(len(o["label"]) <= 8 for o in options):
        per_row = 3

    row: list[InlineKeyboardButton] = []
    for option in options:
        row.append(
            InlineKeyboardButton(
                text=option["label"], callback_data=f"ans:{item.id}:{option['code']}"
            )
        )
        if len(row) == per_row:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append(
        [
            InlineKeyboardButton(text="Не знаю", callback_data=f"skip:{item.id}"),
            InlineKeyboardButton(text="Почему спрашиваю?", callback_data=f"why:{item.id}"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def report_keyboard(period_key: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="По неделям", callback_data=f"weeks:{period_key}"),
            InlineKeyboardButton(text="Советы", callback_data=f"tips:{period_key}"),
        ],
        [
            InlineKeyboardButton(text="Операции", callback_data=f"ops:{period_key}"),
            InlineKeyboardButton(text="Спорные", callback_data="questions"),
        ],
    ]
    url = get_settings().miniapp_url
    if url:
        rows.append(
            [InlineKeyboardButton(text="Открыть экран итогов", web_app=WebAppInfo(url=url))]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def upload_link_keyboard() -> InlineKeyboardMarkup:
    """Отзыв ссылки одним тапом — там же, где её показали.

    Прятать перевыпуск в отдельную команду нельзя: человек вспоминает про
    него ровно в тот момент, когда понял, что ключ куда-то попал.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Выпустить новую", callback_data="upload:rotate")]
        ]
    )


def digest_keyboard(has_questions: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_questions:
        rows.append(
            [InlineKeyboardButton(text="Разобрать спорные", callback_data="questions")]
        )
    rows.append([InlineKeyboardButton(text="Итоги месяца", callback_data="report:month")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
