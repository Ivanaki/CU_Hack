"""Проверка сумм и дат PDF до записи в книгу."""

from __future__ import annotations

import pytest
from PIL import Image

from app.ingest.base import UnknownStatementFormat
from app.ingest.pdf import _alfa, _tbank, parse_pdf_statement

ALFA = """Выписка по счету
Поступления 500,00 RUR
Расходы 300,00 RUR
Неподтвержденные операции 100,00 RUR
Дата проводки Код операции Описание Сумма
в валюте счета
01.09.2026 CRD_A1 Операция по карте: 123456++++++4321, на сумму: 200.00 RUR, \
дата совершения -200,00 RUR
операции: 31.08.26, место совершения операции: 12345678\\RU\\MOSCOW\\CAFE (AP-1234) MCC5812
02.09.2026 C123 Перевод через СБП 500,00 RUR
HOLD Неподтвержденная операция: 888 SHOP -100,00 RUR
А.А. Панченко
АО «АЛЬФА-БАНК»
"""

TBANK = """АКЦИОНЕРНОЕ ОБЩЕСТВО «ТБАНК»
Справка о движении средств
Дата и время Дата Сумма в валюте Сумма операции Описание Номер
операции списания операции в валюте карты операции карты
01.09.2026 02.09.2026 -200.00 ₽ -200.00 ₽ Оплата в CAFE 4321
10:30 11:00 MOSCOW RUS
02.09.2026 02.09.2026 +500.00 ₽ +500.00 ₽ Пополнение. Система 4321
11:15 11:15 быстрых платежей
Пополнения: 500,00 ₽
Расходы: 200,00 ₽
"""


def test_alfa_posted_rows_and_hold_are_reconciled():
    result = _alfa([ALFA])
    assert len(result.rows) == 3
    assert result.pending_count == 1
    card = result.rows[0]
    assert card.booked_at.isoformat() == "2026-08-31T00:00:00"
    assert card.amount == -20000
    assert card.description == "CAFE"
    assert card.mcc == "5812"
    assert card.last4 == "4321"
    assert card.external_id == "CRD_A1"
    assert result.rows[1].last4 == "4321"
    assert result.rows[1].raw["card_inferred"] is True
    assert result.rows[2].amount == -10000
    assert result.rows[2].raw["pending"] is True
    assert result.rows[2].booked_at.isoformat() == "2026-09-02T00:00:00"


def test_alfa_rejects_a_missing_posted_row():
    with pytest.raises(UnknownStatementFormat, match="не сошлись"):
        _alfa([ALFA.replace("-200,00 RUR", "-199,00 RUR")])


def test_alfa_rejects_a_missing_pending_row():
    with pytest.raises(UnknownStatementFormat, match="не сошлась сумма"):
        _alfa([ALFA.replace("HOLD Неподтвержденная операция: 888 SHOP -100,00 RUR", "")])


def test_alfa_hold_without_date_uses_statement_end():
    text = ALFA.replace(
        "Выписка по счету", "Выписка по счету\nс 01.09.2026 по 25.09.2026"
    )
    result = _alfa([text])
    assert result.rows[-1].booked_at.isoformat() == "2026-09-25T00:00:00"
    assert result.rows[-1].raw["date_inferred"] is True


def test_tbank_uses_operation_time_and_card_currency():
    result = _tbank([TBANK])
    assert len(result.rows) == 2
    assert result.rows[0].booked_at.isoformat() == "2026-09-01T10:30:00"
    assert result.rows[0].raw["posting_date"] == "02.09.2026"
    assert result.rows[0].description == "Оплата в CAFE MOSCOW RUS"
    assert result.rows[0].last4 == "4321"


def test_tbank_cashback_without_card_uses_the_statement_card():
    cashback = (
        "03.09.2026 03.09.2026 +3.00 ₽ +3.00 ₽ Кэшбэк за покупки —\n"
        "22:34 22:34\n"
    )
    text = TBANK.replace(
        "Пополнения: 500,00 ₽", cashback + "Пополнения: 503,00 ₽"
    )
    result = _tbank([text])
    assert result.rows[-1].last4 == "4321"
    assert result.rows[-1].raw["card_inferred"] is True


def test_tbank_rejects_a_wrong_total():
    with pytest.raises(UnknownStatementFormat, match="не сошлись"):
        _tbank([TBANK.replace("Расходы: 200,00", "Расходы: 201,00")])


class FakePage:
    def __init__(self, text: str):
        self.text = text

    def extract_text(self):
        return self.text

    def to_image(self, **_kwargs):
        class ImagePage:
            original = Image.new("RGB", (10, 10), "white")

        return ImagePage()


class FakeDocument:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class FakeLLM:
    available = True

    def vision_json(self, *_args, **_kwargs):
        return {
            "bank": "Другой банк",
            "operations": [{
                "date": "2026-09-01", "time": "09:15", "amount": "-10.25",
                "description": "Кофе", "card_last4": "4321",
            }],
        }

    def json(self, *_args, **_kwargs):
        return self.vision_json()


def test_scanned_pdf_uses_vision_and_marks_ai(monkeypatch):
    monkeypatch.setattr(
        "app.ingest.pdf.pdfplumber.open",
        lambda _source: FakeDocument([FakePage("")]),
    )
    result = parse_pdf_statement(b"%PDF-fake", llm=FakeLLM())
    assert result.method == "ai"
    assert len(result.rows) == 1
    assert result.rows[0].amount == -1025
    assert result.rows[0].raw["ai"] is True


def test_unknown_text_pdf_uses_model(monkeypatch):
    text = (
        "Новый банк. Выписка за сентябрь 2026 года.\n"
        "01.09.2026 Покупка кофе -10.25 руб.\n"
        "Других операций на этой странице нет."
    )
    monkeypatch.setattr(
        "app.ingest.pdf.pdfplumber.open",
        lambda _source: FakeDocument([FakePage(text)]),
    )
    result = parse_pdf_statement(b"%PDF-fake", llm=FakeLLM())
    assert result.method == "ai"
    assert result.rows[0].description == "Кофе"


def test_ai_parser_includes_pending(monkeypatch):
    class PendingLLM(FakeLLM):
        def vision_json(self, *_args, **_kwargs):
            result = super().vision_json()
            result["operations"][0]["pending"] = True
            return result

    monkeypatch.setattr(
        "app.ingest.pdf.pdfplumber.open",
        lambda _source: FakeDocument([FakePage("")]),
    )
    result = parse_pdf_statement(b"%PDF-fake", llm=PendingLLM())
    assert result.pending_count == 1
    assert result.rows[0].raw["pending"] is True


def test_invalid_model_amount_is_rejected(monkeypatch):
    class BadLLM(FakeLLM):
        def vision_json(self, *_args, **_kwargs):
            return {"bank": "Банк", "operations": [{
                "date": "2026-09-01", "amount": "-1234.50.00",
                "description": "Кофе",
            }]}

    monkeypatch.setattr(
        "app.ingest.pdf.pdfplumber.open",
        lambda _source: FakeDocument([FakePage("")]),
    )
    with pytest.raises(UnknownStatementFormat, match="неверная дата или сумма"):
        parse_pdf_statement(b"%PDF-fake", llm=BadLLM())


def test_fake_pdf_extension_cannot_bypass_magic_header():
    with pytest.raises(UnknownStatementFormat, match="не PDF"):
        parse_pdf_statement(b"not a document")
