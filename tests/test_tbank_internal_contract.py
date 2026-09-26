"""Формулировки переводов на накопительный счёт из PDF Т-Банка."""

from datetime import datetime

import pytest
from sqlalchemy import select

from app.classify.enrich import purpose_tags, tbank_contract_transfer
from app.classify.pipeline import reclassify
from app.ingest.pdf import _tbank
from app.ingest.service import import_rows
from app.models import Operation, OperationKind, ReviewItem, User


@pytest.fixture(autouse=True)
def _initialized_db(_isolated_db):
    from app.db import init_db

    init_db()


def test_tbank_contract_transfer_from_pdf_is_not_spending_or_question(session):
    # Структура и формулировки строк повторяют предоставленную выписку;
    # номер договора здесь вымышленный.
    statement = """АКЦИОНЕРНОЕ ОБЩЕСТВО «ТБАНК»
Справка о движении средств
Дата и время Дата Сумма в валюте Сумма операции Описание Номер
операции списания операции в валюте карты операции карты
19.09.2026 19.09.2026 -1 500.00 ₽ -1 500.00 ₽ Внутренний перевод на 8673
15:01 15:01 договор 1234567890
20.09.2026 20.09.2026 +200.00 ₽ +200.00 ₽ Перевод с договора 8673
10:15 10:15 1234567890
20.09.2026 20.09.2026 -50.00 ₽ -50.00 ₽ Оплата в CAFE 8673
12:00 12:00 MOSCOW RUS
Пополнения: 200,00 ₽
Расходы: 1 550,00 ₽
"""
    parsed = _tbank([statement])
    assert parsed.method == "template"
    assert parsed.rows[0].description == "Внутренний перевод на договор 1234567890"
    assert parsed.rows[1].description == "Перевод с договора 1234567890"

    user = User(tg_id=990104, display_name="Алексей")
    session.add(user)
    session.flush()
    import_rows(session, user.id, parsed.rows, source=parsed.bank)
    reclassify(session, user.id, now=datetime(2026, 10, 10))

    operations = session.scalars(
        select(Operation).where(Operation.user_id == user.id)
    ).all()
    transfers = [op for op in operations if op.kind == OperationKind.TRANSFER]
    assert len(transfers) == 2
    assert all(op.rule == "own_accounts_by_text" for op in transfers)
    assert all(op.spend_amount == 0 for op in transfers)
    assert all(
        posting.account_kind not in ("expense", "income")
        for op in transfers for posting in op.postings
    )
    assert len([op for op in operations if op.kind == OperationKind.EXPENSE]) == 1
    questions = session.scalars(
        select(ReviewItem).where(ReviewItem.user_id == user.id)
    ).all()
    assert not [question for question in questions if question.operation_id in {
        op.id for op in transfers
    }]


def test_contract_wording_is_specific_to_tbank():
    description = "Внутренний перевод на договор 1234567890"
    assert tbank_contract_transfer(description, "Т-Банк")
    assert "self" in purpose_tags(description, bank="Т-Банк")
    assert not tbank_contract_transfer(description, "Другой банк")
    assert "self" not in purpose_tags(description, bank="Другой банк")
    assert not tbank_contract_transfer("Оплата услуг по договору 1234567890", "Т-Банк")
