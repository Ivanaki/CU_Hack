"""Исходные реквизиты операции, которые помогают вспомнить её смысл."""

from __future__ import annotations

import re
from datetime import datetime

from app.models import Operation

WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
MONTHS_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
PHONE_WITH_COUNTRY_CODE_RE = re.compile(
    r"(?<!\d)(?:\+7|8)[\s().-]*(?:\d[\s().-]*){10}(?!\d)"
)
LABELLED_PHONE_RE = re.compile(
    r"(?:тел(?:ефон)?\.?|номер\s+телефона)\D{0,16}"
    r"((?:(?:\+?7|8)[\s().-]*)?(?:\d[\s().-]*){10})(?!\d)",
    re.IGNORECASE,
)


def question_date_label(value: datetime | None) -> str | None:
    """Дата операции с днём недели; текущий год опускается."""
    if value is None:
        return None
    label = f"{WEEKDAYS[value.weekday()]}, {value.day} {MONTHS_GENITIVE[value.month - 1]}"
    return f"{label} {value.year}" if value.year != datetime.now().year else label


def extract_phone(text: str) -> str | None:
    """Возвращает телефон из текста выписки, сохраняя его исходное написание."""
    text = text or ""
    match = PHONE_WITH_COUNTRY_CODE_RE.search(text)
    if match:
        return match.group(0).strip(" .-()")
    match = LABELLED_PHONE_RE.search(text)
    return match.group(1).strip(" .-()") if match else None


def operation_question_context(operation: Operation | None) -> dict:
    """Имя контрагента и исходные строки выписки по операции-вопросу."""
    if operation is None:
        return {"counterparty": None, "sources": []}

    # У исходящего перевода в первую очередь показываем списание с нашего
    # счёта. Для операций, сведённых из двух выписок, сохраняем обе строки.
    legs = sorted(
        operation.legs,
        key=lambda leg: (leg.amount >= 0, leg.booked_at, leg.id),
    )
    sources = []
    for leg in legs[:2]:
        description = leg.description.strip()
        counterparty_raw = leg.counterparty_raw.strip()
        pieces = list(dict.fromkeys(part for part in (description, counterparty_raw) if part))
        details = " · ".join(pieces)
        account = leg.account
        sources.append(
            {
                "description": details,
                "phone": extract_phone(details),
                "bank": account.bank if account is not None and account.bank else "",
                "filename": leg.batch.filename if leg.batch is not None else "",
            }
        )

    counterparty = operation.counterparty
    return {
        "counterparty": counterparty.display_name if counterparty is not None else None,
        "sources": sources,
    }
