"""Базовый класс ORM и общие соглашения.

Деньги везде — целые копейки (int). Никаких float: 0.1 + 0.2 в отчёте о
финансах — это ровно тот способ потерять доверие к цифрам, о котором
говорит кейс.

Время везде — naive datetime в таймзоне пользователя (по умолчанию
Europe/Moscow). Одна таймзона на установку, зато нет сдвигов на границах
периодов: неделя не начинается в 03:00 понедельника.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}

Money = int  # копейки


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )


def rub(value: float | int) -> Money:
    """Рубли -> копейки. Только для литералов в тестах и сидах."""
    return int(round(float(value) * 100))


def fmt_money(kopecks: Money, *, sign: bool = False) -> str:
    """Копейки -> человекочитаемая строка: 123456 -> '1 234,56 ₽'."""
    neg = kopecks < 0
    whole, frac = divmod(abs(int(kopecks)), 100)
    grouped = f"{whole:,}".replace(",", " ")
    body = grouped if frac == 0 else f"{grouped},{frac:02d}"
    prefix = "-" if neg else ("+" if sign else "")
    return f"{prefix}{body} ₽"
