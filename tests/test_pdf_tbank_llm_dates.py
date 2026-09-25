"""Date format and source-grounding regressions for the PDF LLM path."""

import pytest

from app.ingest.base import UnknownStatementFormat
from app.ingest.pdf import parse_pdf_statement


PAGE_TEXT = (
    "Выписка другого банка за сентябрь 2026 года.\n"
    "25.09.2026 Покупка кофе -10.25 руб.\n"
    "Сведения о движении средств по счету и итоговые данные."
)


class _Page:
    def __init__(self, text):
        self.text = text

    def extract_text(self):
        return self.text


class _Document:
    def __init__(self, text):
        self.pages = [_Page(text)]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class _LLM:
    available = True

    def __init__(self, date):
        self.date = date

    def json(self, prompt, **_kwargs):
        assert "Копируй дату и год дословно" in prompt
        assert "строго как YYYY-MM-DD" in prompt
        assert "25.09.2026" in prompt
        return {
            "bank": "Другой банк",
            "operations": [{
                "date": self.date,
                "time": None,
                "amount": "-10.25",
                "description": "Покупка кофе",
            }],
        }

    def vision_json(self, *_args, **_kwargs):
        raise AssertionError("Текстовая страница должна идти через json")


def _parse(monkeypatch, date, page_text=PAGE_TEXT):
    monkeypatch.setattr(
        "app.ingest.pdf.pdfplumber.open", lambda _source: _Document(page_text)
    )
    return parse_pdf_statement(b"%PDF-fake", llm=_LLM(date))


def test_llm_date_must_use_iso_format(monkeypatch):
    with pytest.raises(UnknownStatementFormat, match="неверная дата или сумма"):
        _parse(monkeypatch, "2026.09.25")


def test_llm_date_with_unsubstantiated_year_is_rejected(monkeypatch):
    with pytest.raises(UnknownStatementFormat, match="не подтверждается текстом PDF"):
        _parse(monkeypatch, "2022-09-25")


def test_llm_accepts_iso_date_supported_by_pdf_text(monkeypatch):
    result = _parse(monkeypatch, "2026-09-25")

    assert result.method == "ai"
    assert result.rows[0].booked_at.isoformat() == "2026-09-25T12:00:00"


def test_llm_date_matches_short_year_date_in_pdf(monkeypatch):
    page_text = (
        "Выписка другого банка за август 2026 года.\n"
        "01.09.2026 Покупка кофе -10.25 руб.\n"
        "Дата операции 31.08.26.\n"
        "Сведения о движении средств по счету и итоговые данные."
    )
    result = _parse(monkeypatch, "2026-08-31", page_text)

    assert result.rows[0].booked_at.isoformat() == "2026-08-31T12:00:00"
