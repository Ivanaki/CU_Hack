"""Разбор выписок. Один парсер с картами колонок под каждый банк.

Форматы банков отличаются заголовками, разделителем, кодировкой и видом
числа — но не смыслом. Поэтому вместо четырёх парсеров здесь один движок и
четыре описания колонок: добавить банк стоит десяти строк, а не файла.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import datetime

import chardet

from app.ledger.accounts import norm_key
from app.models import Money


@dataclass(slots=True)
class ParsedRow:
    """Нормализованная строка выписки, ещё не привязанная к БД."""

    booked_at: datetime
    amount: Money  # знаковые копейки: + приход на счёт, - расход
    description: str = ""
    counterparty_raw: str = ""
    mcc: str | None = None
    currency: str = "RUB"
    bank: str = ""
    last4: str | None = None
    account_name: str | None = None
    external_id: str | None = None
    category_hint: str | None = None
    raw: dict = field(default_factory=dict)

    def dedup_hash(self) -> str:
        """Ключ дедупликации.

        Время округляем до минуты, а не до секунды: одна и та же операция в
        выписках двух банков и в пуш-уведомлении датируется чуть по-разному.
        Описание нормализуем, потому что банки по-разному режут пробелы
        и регистр. Это закрывает пункт кейса про импорт из нескольких банков
        с устранением дублей.
        """
        stamp = self.booked_at.strftime("%Y-%m-%dT%H:%M")
        parts = [
            self.bank.lower(),
            self.last4 or "",
            stamp,
            str(self.amount),
            norm_key(self.description)[:64],
        ]
        if self.external_id:
            parts = [self.bank.lower(), "id", self.external_id]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:40]


# --- описания форматов -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class ColumnMap:
    bank: str
    delimiter: str
    encoding: str
    datetime_col: str
    datetime_formats: tuple[str, ...]
    amount_col: str
    description_col: str
    mcc_col: str | None = None
    card_col: str | None = None
    currency_col: str | None = None
    category_col: str | None = None
    counterparty_col: str | None = None
    external_id_col: str | None = None
    status_col: str | None = None
    ok_statuses: tuple[str, ...] = ()
    # Подпись формата: набор заголовков, по которым узнаём банк.
    signature: tuple[str, ...] = ()


FORMATS: tuple[ColumnMap, ...] = (
    ColumnMap(
        bank="Т-Банк",
        delimiter=";",
        encoding="cp1251",
        datetime_col="Дата операции",
        datetime_formats=("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"),
        amount_col="Сумма операции",
        description_col="Описание",
        mcc_col="MCC",
        card_col="Номер карты",
        currency_col="Валюта операции",
        category_col="Категория",
        status_col="Статус",
        ok_statuses=("ok", "ок"),
        signature=("Дата операции", "Сумма операции", "Описание"),
    ),
    ColumnMap(
        bank="Сбербанк",
        delimiter=";",
        encoding="cp1251",
        datetime_col="Дата операции",
        datetime_formats=("%d.%m.%Y %H:%M", "%d.%m.%Y"),
        amount_col="Сумма в валюте счёта",
        description_col="Описание операции",
        category_col="Категория",
        card_col="Номер карты",
        signature=("Сумма в валюте счёта", "Описание операции"),
    ),
    ColumnMap(
        bank="Альфа-Банк",
        delimiter=";",
        encoding="utf-8-sig",
        datetime_col="Дата операции",
        datetime_formats=("%d.%m.%Y", "%d.%m.%Y %H:%M:%S"),
        amount_col="Сумма",
        description_col="Назначение платежа",
        card_col="Номер счета",
        currency_col="Валюта",
        signature=("Назначение платежа", "Номер счета"),
    ),
    # Наш канонический формат: им описан демонстрационный набор и в него же
    # выгружается всё, что пользователь захочет унести с собой.
    ColumnMap(
        bank="",
        delimiter=",",
        encoding="utf-8-sig",
        datetime_col="datetime",
        datetime_formats=("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"),
        amount_col="amount",
        description_col="description",
        mcc_col="mcc",
        card_col="card",
        currency_col="currency",
        counterparty_col="counterparty",
        external_id_col="external_id",
        signature=("datetime", "amount", "description"),
    ),
)


class UnknownStatementFormat(ValueError):
    pass


def _decode(data: bytes, preferred: str) -> str:
    for enc in (preferred, "utf-8-sig", "utf-8", "cp1251"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    guess = chardet.detect(data).get("encoding") or "utf-8"
    return data.decode(guess, errors="replace")


_AMOUNT_RE = re.compile(r"[^0-9,.\-+]")


def parse_amount(value: str) -> Money:
    """Строка суммы -> копейки. Терпит пробелы, запятые и валютные символы."""
    text = _AMOUNT_RE.sub("", (value or "").strip())
    if not text:
        return 0
    # "1.234,56" (европейский) против "1,234.56" (английский)
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        return int(round(float(text) * 100))
    except ValueError:
        return 0


def parse_last4(value: str | None) -> str | None:
    digits = re.findall(r"\d{4}", value or "")
    return digits[-1] if digits else None


def _parse_dt(value: str, formats: tuple[str, ...]) -> datetime | None:
    text = (value or "").strip()
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def detect_format(header: list[str]) -> ColumnMap | None:
    normalized = {h.strip().lower().replace("﻿", "") for h in header}
    best: ColumnMap | None = None
    best_score = 0
    for fmt in FORMATS:
        need = {s.lower() for s in fmt.signature}
        if need <= normalized and len(need) > best_score:
            best, best_score = fmt, len(need)
    return best


def _sniff_delimiter(text: str) -> str:
    first = text.splitlines()[0] if text.splitlines() else ""
    counts = {d: first.count(d) for d in (";", ",", "\t")}
    return max(counts, key=lambda d: counts[d]) if max(counts.values()) else ","


def parse_statement(
    data: bytes, *, filename: str = "", bank_hint: str | None = None
) -> tuple[str, list[ParsedRow]]:
    """Байты CSV -> (код банка, строки). Формат определяется по заголовку."""
    text = _decode(data, "utf-8-sig")
    delimiter = _sniff_delimiter(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    header = [h or "" for h in (reader.fieldnames or [])]
    fmt = detect_format(header)
    if fmt is None:
        raise UnknownStatementFormat(
            f"не узнал формат выписки {filename!r}; колонки: {header[:8]}"
        )

    bank = bank_hint or fmt.bank or _bank_from_filename(filename) or "Банк"
    rows: list[ParsedRow] = []
    for record in reader:
        row = _row_from(record, fmt, bank)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda r: r.booked_at)
    return bank, rows


def _bank_from_filename(filename: str) -> str | None:
    stem = (filename or "").lower()
    for token, name in (
        ("tbank", "Т-Банк"), ("tinkoff", "Т-Банк"), ("sber", "Сбербанк"),
        ("alfa", "Альфа-Банк"), ("vtb", "ВТБ"), ("wb", "Wildberries"),
        ("ozon", "Ozon"), ("raif", "Райффайзен"),
    ):
        if token in stem:
            return name
    return None


def _row_from(record: dict[str, str], fmt: ColumnMap, bank: str) -> ParsedRow | None:
    def cell(col: str | None) -> str:
        if not col:
            return ""
        for key, value in record.items():
            if key and key.strip().lower().replace("﻿", "") == col.lower():
                return (value or "").strip()
        return ""

    if fmt.status_col and fmt.ok_statuses:
        status = cell(fmt.status_col).lower()
        if status and status not in fmt.ok_statuses:
            return None

    booked_at = _parse_dt(cell(fmt.datetime_col), fmt.datetime_formats)
    if booked_at is None:
        return None
    amount = parse_amount(cell(fmt.amount_col))
    if amount == 0:
        return None

    row_bank = cell("bank") or bank
    return ParsedRow(
        booked_at=booked_at,
        amount=amount,
        description=cell(fmt.description_col),
        counterparty_raw=cell(fmt.counterparty_col) or cell(fmt.description_col),
        mcc=(cell(fmt.mcc_col) or None),
        currency=(cell(fmt.currency_col) or "RUB").upper()[:3],
        bank=row_bank,
        last4=parse_last4(cell(fmt.card_col)),
        external_id=(cell(fmt.external_id_col) or None),
        category_hint=(cell(fmt.category_col) or None),
        raw=dict(record),
    )
