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
RU_WEEKDAYS = (
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье",
)
RU_WEEKDAYS_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

# Гранулярность разбиения периода на срезы.
DAY, WEEK, MONTH = "day", "week", "month"
GRANULARITY_TITLE = {DAY: "По дням", WEEK: "По неделям", MONTH: "По месяцам"}


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

    @property
    def starts_at_midnight(self) -> bool:
        return self.start == datetime(self.start.year, self.start.month, self.start.day)

    @property
    def is_calendar_day(self) -> bool:
        return self.starts_at_midnight and self.end == self.start + timedelta(days=1)

    @property
    def is_calendar_week(self) -> bool:
        return (
            self.starts_at_midnight
            and self.start.weekday() == 0
            and self.end == self.start + timedelta(days=7)
        )

    @property
    def is_calendar_month(self) -> bool:
        if not self.starts_at_midnight or self.start.day != 1:
            return False
        nxt = datetime(self.start.year + (self.start.month == 12), (self.start.month % 12) + 1, 1)
        return self.end == nxt

    @property
    def is_calendar_year(self) -> bool:
        return self.start == datetime(self.start.year, 1, 1) and self.end == datetime(
            self.start.year + 1, 1, 1
        )

    @property
    def unit(self) -> str:
        """Как назвать сам период в винительном падеже: «складываются в месяц»."""
        if self.is_calendar_day:
            return "день"
        if self.is_calendar_week:
            return "неделю"
        if self.is_calendar_month:
            return "месяц"
        if self.is_calendar_year:
            return "год"
        return "период"

    def shifted_back(self) -> Period:
        """Период для сравнения — предыдущий такой же.

        Для календарного месяца это предыдущий календарный месяц, а не окно
        такой же длины: у сентября 30 дней, и сдвиг на 30 дней назад дал бы
        «2 августа – 1 сентября». Формально это «предыдущие 30 дней», но на
        экране написано «в прошлом месяце», и человек сравнивает сентябрь с
        августом, а не с окном, из которого выпало 1 августа.

        Для года — предыдущий год, для календарной недели и дня — предыдущие
        неделя и день (у них длина и так постоянная, но нужен человеческий
        заголовок). Для всего остального остаётся окно равной длины.
        """
        if self.is_calendar_month:
            previous = self.start - timedelta(days=1)
            return month(previous.year, previous.month)
        if self.is_calendar_year:
            return year(self.start.year - 1)
        if self.is_calendar_week:
            return week((self.start - timedelta(days=1)).date())
        if self.is_calendar_day:
            return day((self.start - timedelta(days=1)).date())
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


def granularity(period: Period) -> str:
    """Чем резать период, чтобы столбиков было видно, а не 53 штуки.

    День и неделя режутся днями, месяц и квартал — неделями, год — месяцами.
    Именно из-за отсутствия этого правила годовой период отдавал наружу 53
    недельных среза, которые на экране в 390 точек не помещаются.
    """
    if period.days <= 10:
        return DAY
    if period.days <= 92:
        return WEEK
    return MONTH


def split_auto(period: Period) -> tuple[str, list[Period]]:
    """Период и его срезы подходящей длины. Сумма срезов равна периоду."""
    kind = granularity(period)
    cut = {DAY: split_into_days, WEEK: split_into_weeks, MONTH: split_into_months}[kind]
    return kind, cut(period)


def parse_period(spec: str, *, today: date | None = None) -> Period:
    """Разбирает человеческое указание периода: день, неделя, месяц, год."""
    today = today or date.today()
    key = (spec or "").strip().lower()
    if key.isdigit() and len(key) == 4:  # 2026
        return year(int(key))
    if len(key) == 10 and key.count("-") == 2:  # 2026-09-15
        y, m, d = key.split("-")
        return day(date(int(y), int(m), int(d)))
    if len(key) == 8 and key[4:6] == "-w":  # 2026-W38, стрелка «неделя назад»
        y, w = key.split("-w")
        return week(date.fromisocalendar(int(y), int(w), 1))
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
