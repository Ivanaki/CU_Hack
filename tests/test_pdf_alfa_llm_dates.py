"""Регрессия: LLM выбирает дату операции вместо даты проводки Альфы."""

from app.ingest.pdf import parse_pdf_statement


class _Page:
    def __init__(self, text: str):
        self.text = text

    def extract_text(self):
        return self.text


class _Document:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_alfa_llm_uses_operation_date_when_posting_date_differs(monkeypatch):
    text = (
        "Выписка банка, операции за август и сентябрь 2026 года.\n"
        "01.09.2026 CRD_A1 Покупка в CAFE -200.00 RUR\n"
        "Дата совершения операции: 31.08.2026.\n"
        "Раздел с операциями по счёту и описанием платежей."
    )
    monkeypatch.setattr(
        "app.ingest.pdf.pdfplumber.open",
        lambda _source: _Document([_Page(text)]),
    )

    class _LLM:
        available = True

        def json(self, prompt, **_kwargs):
            assert "дату совершения операции" in prompt
            assert "а не дату проводки/отражения" in prompt
            assert "31.08.2026" in prompt
            return {
                "bank": "Альфа-Банк",
                "operations": [{
                    "date": "2026-08-31",
                    "time": None,
                    "amount": "-200.00",
                    "description": "CAFE",
                    "card_last4": None,
                    "pending": False,
                }],
            }

        def vision_json(self, *_args, **_kwargs):
            raise AssertionError("Текстовая страница должна идти через json")

    result = parse_pdf_statement(b"%PDF-fake", llm=_LLM())

    assert result.method == "ai"
    assert len(result.rows) == 1
    assert result.rows[0].booked_at.isoformat() == "2026-08-31T12:00:00"
