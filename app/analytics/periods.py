"""Периоды и их разбиение.

Кейс требует, чтобы недельные срезы складывались в месячный. Это свойство
обеспечивается здесь: разбиение месяца на недели даёт непересекающиеся
интервалы, объединение которых точно равно месяцу. Неполные недели по краям
обрезаются границей месяца, а не выкидываются и не выходят за неё.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta

RU_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
RU_MONTHS_NOM = (
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)


@dataclass(frozen=True, slots=True)
class Period:
    """Полуинтервал [start, end): конец не включается.

    Полуинтервалы — единственный способ нарезать время без дыр и нахлёстов.
    Операция в 23:59:59 последнего дня попадает ровно в один период.
    """

    start: datetime
    end: datetime
    label: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def shifted_back(self) -> Period:
        """Предыдущий период такой же длины — для сравнения."""
        delta = self.end - self.start
        return Period(self.start - delta, self.start, f"предыдущие {self.days} дн.")


def day(d: date) -> Period:
    start = datetime(d.year, d.month, d.day)
    return Period(start, start + timedelta(days=1), f"{d.day} {RU_MONTHS[d.month - 1]}")


def week(d: date) -> Period:
    monday = d - timedelta(days=d.weekday())
    start = datetime(monday.year, monday.month, monday.day)
    end = start + timedelta(days=7)
    last = (end - timedelta(days=1)).date()
    return Period(start, end, f"{monday.day}–{last.day} {RU_MONTHS[last.month - 1]}")


def month(year: int, m: int) -> Period:
    start = datetime(year, m, 1)
    end = datetime(year + (m == 12), (m % 12) + 1, 1)
    return Period(start, end, f"{RU_MONTHS_NOM[m - 1]} {year}")


def year(y: int) -> Period:
    return Period(datetime(y, 1, 1), datetime(y + 1, 1, 1), str(y))


def custom(start: date, end_inclusive: date) -> Period:
    s = datetime(start.year, start.month, start.day)
    e = datetime(end_inclusive.year, end_inclusive.month, end_inclusive.day) + timedelta(days=1)
    return Period(s, e, f"{start:%d.%m} – {end_inclusive:%d.%m}")


def split_into_weeks(period: Period) -> list[Period]:
    """Режет период на недельные куски, обрезанные его границами.

    Сумма по кускам равна сумме по периоду: куски не пересекаются и
    покрывают период целиком.
    """
    out: list[Period] = []
    cursor = period.start
    while cursor < period.end:
        monday = cursor - timedelta(days=cursor.weekday())
        monday = datetime(monday.year, monday.month, monday.day)
        chunk_end = min(monday + timedelta(days=7), period.end)
        chunk_start = max(monday, cursor)
        last = (chunk_end - timedelta(seconds=1)).date()
        first = chunk_start.date()
        label = (
            f"{first.day}–{last.day} {RU_MONTHS[last.month - 1]}"
            if first.month == last.month
            else f"{first.day} {RU_MONTHS[first.month - 1]} – "
            f"{last.day} {RU_MONTHS[last.month - 1]}"
        )
        out.append(Period(chunk_start, chunk_end, label))
        cursor = chunk_end
    return out


def split_into_months(period: Period) -> list[Period]:
    out: list[Period] = []
    cursor = period.start
    while cursor < period.end:
        _, last_day = calendar.monthrange(cursor.year, cursor.month)
        month_end = datetime(cursor.year, cursor.month, last_day) + timedelta(days=1)
        chunk_end = min(month_end, period.end)
        out.append(
            Period(cursor, chunk_end, f"{RU_MONTHS_NOM[cursor.month - 1]} {cursor.year}")
        )
        cursor = chunk_end
    return out


def split_into_days(period: Period) -> list[Period]:
    out: list[Period] = []
    cursor = period.start
    while cursor < period.end:
        chunk_end = min(cursor + timedelta(days=1), period.end)
        out.append(Period(cursor, chunk_end, f"{cursor.day} {RU_MONTHS[cursor.month - 1]}"))
        cursor = chunk_end
    return out


def parse_period(spec: str, *, today: date | None = None) -> Period:
    """Разбирает человеческое указание периода: день, неделя, месяц, год."""
    today = today or date.today()
    key = (spec or "").strip().lower()
    if key in {"день", "сегодня", "day", "today"}:
        return day(today)
    if key in {"вчера", "yesterday"}:
        return day(today - timedelta(days=1))
    if key in {"неделя", "week"}:
        return week(today)
    if key in {"прошлая неделя", "last_week"}:
        return week(today - timedelta(days=7))
    if key in {"месяц", "month"}:
        return month(today.year, today.month)
    if key in {"прошлый месяц", "last_month"}:
        prev = today.replace(day=1) - timedelta(days=1)
        return month(prev.year, prev.month)
    if key in {"год", "year"}:
        return year(today.year)
    if key.count("-") == 1 and len(key) == 7:  # 2026-09
        y, m = key.split("-")
        return month(int(y), int(m))
    raise ValueError(f"не понял период: {spec!r}")
