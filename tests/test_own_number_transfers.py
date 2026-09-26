"""Перевод по собственному номеру между выписками разных банков."""

from datetime import datetime

import pytest
from sqlalchemy import select

from app.classify.enrich import own_number_transfer, purpose_tags
from app.classify.pipeline import reclassify
from app.ingest.base import ParsedRow, parse_statement
from app.ingest.service import import_rows
from app.models import Operation, OperationKind, RawTransaction, ReviewItem, User


@pytest.fixture(autouse=True)
def _initialized_db(_isolated_db):
    from app.db import init_db

    init_db()


def _user(session, tg_id):
    user = User(tg_id=tg_id, display_name="Алексей")
    session.add(user)
    session.flush()
    return user


def _operations(session, user):
    return list(session.scalars(select(Operation).where(Operation.user_id == user.id)).all())


def test_two_bank_statements_merge_own_number_in_either_upload_order(session):
    outgoing_csv = (
        "Дата операции;Сумма операции;Описание;Номер карты\n"
        '26.09.2026 10:00:00;-10000,00;Перевод по номеру телефона;*4127\n'
    ).encode("cp1251")
    outgoing_marked_csv = outgoing_csv.replace(
        "Перевод по номеру телефона".encode("cp1251"),
        "Перевод на номер «Мой номер»".encode("cp1251"),
    )
    incoming_csv = (
        "Дата операции;Номер счета;Сумма;Валюта;Назначение платежа\n"
        '26.09.2026;40817810900000005531;10000,00;RUB;Перевод с номера "Мой номер"\n'
    ).encode()
    incoming_generic_csv = incoming_csv.replace(
        'Перевод с номера "Мой номер"'.encode(), "Перевод по СБП".encode()
    )

    for tg_id, files in enumerate(
        ((outgoing_csv, incoming_csv), (incoming_generic_csv, outgoing_marked_csv)),
        start=990100,
    ):
        user = _user(session, tg_id)
        for data in files:
            bank, rows = parse_statement(data)
            import_rows(session, user.id, rows, source=bank)
            reclassify(session, user.id, now=datetime(2026, 10, 10))

        operations = _operations(session, user)
        assert len(operations) == 1
        transfer = operations[0]
        assert transfer.kind == OperationKind.TRANSFER
        assert transfer.rule == "self_transfer"
        assert transfer.spend_amount == 0
        assert len(transfer.legs) == 2
        assert {leg.account.bank for leg in transfer.legs} == {"Т-Банк", "Альфа-Банк"}
        assert not session.scalars(
            select(ReviewItem).where(ReviewItem.user_id == user.id)
        ).all()
        assert not [
            p for p in transfer.postings if p.account_kind in ("expense", "income")
        ]


def test_own_number_without_second_statement_stays_transfer(session):
    user = _user(session, 990102)
    row = ParsedRow(
        booked_at=datetime(2026, 9, 26, 10), amount=100_000,
        description="Зачисление с номера 'Мой номер'", bank="Альфа-Банк",
        last4="5531",
    )
    import_rows(session, user.id, [row], source="Альфа-Банк")
    reclassify(session, user.id, now=datetime(2026, 10, 10))

    operations = _operations(session, user)
    assert len(operations) == 1
    assert operations[0].kind == OperationKind.TRANSFER
    assert operations[0].rule == "own_accounts_by_text"
    assert operations[0].spend_amount == 0
    assert not [p for p in operations[0].postings if p.account_kind == "income"]
    assert not session.scalars(select(ReviewItem).where(ReviewItem.user_id == user.id)).all()


def test_generic_phone_transfer_or_purchase_is_not_own_number(session):
    assert not own_number_transfer("Перевод по номеру телефона +7 900 123-45-67")
    assert not own_number_transfer("Оплата связи на мой номер телефона")
    assert "self" not in purpose_tags("Перевод по номеру телефона +7 900 123-45-67")

    user = _user(session, 990103)
    rows = [
        ParsedRow(
            booked_at=datetime(2026, 9, 26, 10), amount=-100_000,
            description="Перевод Дмитрий К. по номеру телефона", bank="Т-Банк",
            last4="4127",
        ),
        ParsedRow(
            booked_at=datetime(2026, 9, 26, 10, 5), amount=100_000,
            description="Перевод от Ольга С. по номеру телефона", bank="Альфа-Банк",
            last4="5531",
        ),
    ]
    import_rows(session, user.id, rows, source="Два банка")
    reclassify(session, user.id, now=datetime(2026, 10, 10))
    assert len(_operations(session, user)) == 2
    assert not session.scalars(
        select(Operation).where(
            Operation.user_id == user.id, Operation.kind == OperationKind.TRANSFER
        )
    ).all()
    assert len(session.scalars(
        select(RawTransaction).where(RawTransaction.user_id == user.id)
    ).all()) == 2
