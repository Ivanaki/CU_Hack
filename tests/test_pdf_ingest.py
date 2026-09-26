"""Проверка сумм и дат PDF до записи в книгу."""

from __future__ import annotations

import pytest
from PIL import Image

from app.classify.enrich import category_from_text
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


def test_alfa_rejects_hold_when_pending_total_is_missing():
    text = ALFA.replace("Неподтвержденные операции 100,00 RUR\n", "")
    with pytest.raises(UnknownStatementFormat, match="не найден итог"):
        _alfa([text])


def test_alfa_without_pending_section_uses_template_parser(monkeypatch):
    text = (
        "Выписка по счету\nАЛЬФА-БАНК\n"
        + ALFA.replace("Неподтвержденные операции 100,00 RUR\n", "")
        .replace("Расходы 300,00 RUR\n", "Расходы 200,00 RUR\n")
        .replace("HOLD Неподтвержденная операция: 888 SHOP -100,00 RUR\nА.А. Панченко\n", "")
    )
    monkeypatch.setattr(
        "app.ingest.pdf.pdfplumber.open",
        lambda _source: FakeDocument([FakePage(text)]),
    )

    result = parse_pdf_statement(b"%PDF-fake", llm=FakeLLM())

    assert result.method == "template"
    assert len(result.rows) == 2
    assert result.pending_count == 0


def test_alfa_parses_tirr_rows_with_hash_in_operation_code():
    text = """Выписка по счету
Поступления 524,89 RUR
Расходы 25,05 RUR
Дата проводки Код операции Описание Сумма
в валюте счета
01.09.2026 C123 Пополнение 500,00 RUR
02.09.2026 TIRR#26080702265 Корректировка 24,89 RUR
03.09.2026 TIRR#26080702265 Погашение -24,89 RUR
04.09.2026 TIRR#26080702265 Проценты -0,16 RUR
АО «АЛЬФА-БАНК»
"""

    result = _alfa([text])

    assert len(result.rows) == 4
    assert [row.amount for row in result.rows[1:]] == [2489, -2489, -16]
    assert [row.external_id for row in result.rows[1:]] == [
        "TIRR#26080702265",
        "TIRR#26080702265",
        "TIRR#26080702265",
    ]


def test_alfa_hold_without_date_uses_statement_end():
    text = ALFA.replace(
        "Выписка по счету", "Выписка по счету\nс 01.09.2026 по 25.09.2026"
    )
    result = _alfa([text])
    assert result.rows[-1].booked_at.isoformat() == "2026-09-25T00:00:00"
    assert result.rows[-1].raw["date_inferred"] is True


def test_alfa_hold_uses_matching_companion_operation_date():
    text = ALFA.replace(
        "HOLD Неподтвержденная операция: 888 SHOP -100,00 RUR\nА.А. Панченко",
        "HOLD Неподтвержденная операция: 888 SHOP -100,00 RUR\n"
        "SHOP>CAFE 31.08.26 31.08.26 100.00 RUR 220015++++++4321 (ALFA PAY-1234),\n"
        "дата операции: 31.08.2026, дата предполагаемого снятия блокировки:\n"
        "10.09.2026 Сумма, зарезервированная для погашения: 0.00\n"
        "А.А. Панченко",
    )

    result = _alfa([text])

    assert result.rows[-1].booked_at.isoformat() == "2026-08-31T00:00:00"
    assert result.rows[-1].raw["date_inferred"] is False


def test_alfa_normalizes_different_hold_merchant_formats():
    text = """Выписка по счету
Поступления 0,00 RUR
Расходы 2 229,72 RUR
Неподтвержденные операции 2 219,72 RUR
Дата проводки Код операции Описание Сумма
в валюте счета
01.09.2026 CRD_A1 Операция по карте: ++++++4321, на сумму: 10.00 RUR, дата совершения -10,00 RUR
операции: 31.08.26, место совершения операции: 36259933\\RU\\MOSCOW\\CH77420 (AP-1234) MCC5411
HOLD Неподтвержденная операция: 51K822 36259933 RU CH77420>MOSCOW 24.09.26 -316,75 RUR
24.09.26 316.75 RUR 220015++++++4321, дата операции: 24.09.2026
HOLD Неподтвержденная операция: 2052UW 42855799 RU T Vending>Moskva 24.09.26 -185,00 RUR
24.09.26 185.00 RUR 220015++++++4321, дата операции: 24.09.2026
HOLD Неподтвержденная операция: 2U17U0 40539264 RU INTER -95,00 RUR
STOLOVAYA>MOSCOW 24.09.26 24.09.26 95.00 RUR 220015++++++4321, дата операции: 24.09.2026
HOLD Неподтвержденная операция: 4WS302 33735090 RU MAGNIT MM -337,97 RUR
MULTIZITER>MO24.09.26 24.09.26 337.97 RUR 220015++++++4321, дата операции: 24.09.2026
HOLD Неподтвержденная операция: 36451E 07470779 RU LK METRO>Moskva 25.09.26 -300,00 RUR
25.09.26 300.00 RUR 220015++++++4321, дата операции: 25.09.2026
HOLD Неподтвержденная операция: 7C64WN 07770562 RU METRO>Moskva 25.09.26 -720,00 RUR
25.09.26 720.00 RUR 220015++++++4321, дата операции: 25.09.2026
HOLD Неподтвержденная операция: 7W484U 42855799 RU T Vending>Moskva 25.09.26 -265,00 RUR
25.09.26 265.00 RUR 220015++++++4321, дата операции: 25.09.2026
АО «АЛЬФА-БАНК»
"""

    result = _alfa([text])

    assert result.pending_count == 7
    assert [row.description for row in result.rows[:1]] == ["Чижик"]
    assert [row.description for row in result.rows[-7:]] == [
        "Чижик", "Торговый автомат", "Интер", "Магнит",
        "Метро Москвы", "METRO", "Торговый автомат",
    ]
    assert category_from_text("Чижик") == "groceries"
    assert category_from_text("Торговый автомат") == "delivery"
    assert category_from_text("Интер") == "delivery"
    assert category_from_text("Метро Москвы") == "transport"
    assert category_from_text("METRO") == "groceries"
    assert [row.booked_at.isoformat() for row in result.rows[-7:]] == [
        "2026-09-24T00:00:00",
        "2026-09-24T00:00:00",
        "2026-09-24T00:00:00",
        "2026-09-24T00:00:00",
        "2026-09-25T00:00:00",
        "2026-09-25T00:00:00",
        "2026-09-25T00:00:00",
    ]


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
