"""Разбор выписок и дедупликация.

Кейс требует «импорт из нескольких банков одновременно с устранением
дублей». Здесь проверяются оба уровня: точный по хешу и мягкий по смыслу,
между источниками разного типа.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.ingest.base import ParsedRow, parse_amount, parse_statement
from app.ingest.service import import_rows
from app.models import Decision, Operation, OperationSource, RawTransaction


def row(
    *, when: datetime, amount: int, text: str, bank: str = "Т-Банк", last4: str = "4127"
) -> ParsedRow:
    return ParsedRow(
        booked_at=when,
        amount=amount,
        description=text,
        counterparty_raw=text,
        bank=bank,
        last4=last4,
    )


def count_raw(session, user_id: int) -> int:
    from sqlalchemy import func, select

    return int(
        session.scalar(
            select(func.count()).select_from(RawTransaction).where(
                RawTransaction.user_id == user_id
            )
        )
        or 0
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1 234,56", 123_456),
        ("-5 480.00", -548_000),
        ("1.234,56", 123_456),
        ("1,234.56", 123_456),
        ("3240", 324_000),
        ("", 0),
        ("—", 0),
    ],
)
def test_суммы_разбираются_в_копейки(text: str, expected: int):
    """Деньги — целые копейки. Любой формат банка приводится к ним без float."""
    assert parse_amount(text) == expected


def test_форматы_трёх_банков_узнаются_по_заголовку(seeded):
    from pathlib import Path

    demo = Path(__file__).resolve().parent.parent / "data" / "demo"
    for filename, expected_bank in (
        ("tbank_2026-07_09.csv", "Т-Банк"),
        ("alfa_2026-09.csv", "Альфа-Банк"),
    ):
        bank, rows = parse_statement((demo / filename).read_bytes(), filename=filename)
        assert bank == expected_bank, filename
        assert rows, f"{filename}: не разобрана ни одна строка"
        assert all(r.amount != 0 for r in rows)


def test_повторная_загрузка_той_же_выписки_ничего_не_дублирует(session, _isolated_db):
    """Точная дедупликация: файл можно залить дважды без последствий."""
    from app.models import User

    user = User(tg_id=5001, display_name="Дубли")
    session.add(user)
    session.flush()

    when = datetime(2026, 9, 10, 12, 0)
    batch = [
        row(when=when, amount=-324_000, text="PYATEROCHKA 4312"),
        row(when=when + timedelta(hours=3), amount=-89_000, text="SURF COFFEE"),
    ]
    first = import_rows(session, user.id, batch, source="Т-Банк")
    second = import_rows(session, user.id, batch, source="Т-Банк")

    assert len(first.new_rows) == 2
    assert len(second.new_rows) == 0
    assert second.duplicates == 2
    assert count_raw(session, user.id) == 2


def test_скриншот_и_выписка_про_одну_покупку_считаются_один_раз(session, _isolated_db):
    """Мягкая дедупликация между источниками.

    Пользователь прислал скриншот в день покупки, а через месяц залил
    выписку. Это один факт, а не два, и выигрывает выписка как более
    точный источник.
    """
    from app.models import User

    user = User(tg_id=5002, display_name="Скрин и выписка")
    session.add(user)
    session.flush()

    # Днём пришёл скриншот: название продавца короткое, время неточное.
    import_rows(
        session,
        user.id,
        [row(when=datetime(2026, 9, 10, 16, 40), amount=-231_000, text="Wildberries")],
        source="Т-Банк",
        origin=OperationSource.SCREENSHOT,
    )
    assert count_raw(session, user.id) == 1

    # Через месяц приехала выписка: та же сумма, тот же день, полное название.
    result = import_rows(
        session,
        user.id,
        [
            row(
                when=datetime(2026, 9, 10, 16, 44),
                amount=-231_000,
                text="WILDBERRIES заказ 8812",
            )
        ],
        source="Т-Банк",
        origin=OperationSource.IMPORT,
    )

    assert result.superseded == 1, "выписка должна вытеснить запись из скриншота"
    assert count_raw(session, user.id) == 1, "операция не должна задвоиться"

    survivor = session.query(RawTransaction).filter_by(user_id=user.id).one()
    assert survivor.source == OperationSource.IMPORT
    assert "заказ" in survivor.description


def test_две_одинаковые_покупки_за_день_остаются_двумя(session, _isolated_db):
    """Два кофе по одной цене в один день — это две траты, а не дубль.

    Мягкая дедупликация не должна их склеивать: она срабатывает только
    между источниками разного типа.
    """
    from app.models import User

    user = User(tg_id=5003, display_name="Два кофе")
    session.add(user)
    session.flush()

    result = import_rows(
        session,
        user.id,
        [
            row(when=datetime(2026, 9, 10, 9, 15), amount=-32_000, text="SURF COFFEE"),
            row(when=datetime(2026, 9, 10, 18, 40), amount=-32_000, text="SURF COFFEE"),
        ],
        source="Т-Банк",
        origin=OperationSource.IMPORT,
    )
    assert len(result.new_rows) == 2
    assert result.duplicates == 0
    assert count_raw(session, user.id) == 2


def test_pending_is_counted_then_replaced_without_losing_answer(session, _isolated_db):
    from sqlalchemy import select

    from app.api.routes import _pending_operation_ids
    from app.api.schemas import OperationOut
    from app.classify.pipeline import DecisionKind, reclassify
    from app.models import User

    user = User(tg_id=5091, display_name="Тест")
    session.add(user)
    session.flush()

    hold = row(
        when=datetime(2026, 9, 24), amount=-10_000,
        text="CAFE", bank="Альфа-Банк", last4="4321",
    )
    hold.raw["pending"] = True
    first = import_rows(session, user.id, [hold], source="Альфа-Банк")
    assert len(first.new_rows) == 1
    reclassify(session, user.id)
    pending_op = session.scalar(select(Operation).where(Operation.user_id == user.id))
    assert pending_op.spend_amount == 10_000
    assert OperationOut.of(
        pending_op,
        is_pending=pending_op.id in _pending_operation_ids(session, user.id, [pending_op.id]),
    ).is_pending

    session.add(Decision(
        user_id=user.id, anchor_hash=hold.dedup_hash(),
        kind=DecisionKind.SET_CATEGORY, payload={"category": "cafe"},
    ))
    session.flush()

    posted = row(
        when=datetime(2026, 9, 26), amount=-10_200,
        text="CAFE MOSCOW", bank="Альфа-Банк", last4="4321",
    )
    posted.external_id = "CRD_123"
    second = import_rows(session, user.id, [posted], source="Альфа-Банк")
    assert second.superseded == 1
    assert len(second.new_rows) == 1
    reclassify(session, user.id)
    raws = session.scalars(select(RawTransaction).where(RawTransaction.user_id == user.id)).all()
    assert len(raws) == 1
    assert raws[0].dedup_hash == posted.dedup_hash()
    assert raws[0].is_pending is False
    decision = session.scalar(select(Decision).where(Decision.user_id == user.id))
    assert decision.anchor_hash == posted.dedup_hash()
    final_op = session.scalar(select(Operation).where(Operation.user_id == user.id))
    assert final_op.spend_amount == 10_200
    assert final_op.category_code == "cafe"
    assert _pending_operation_ids(session, user.id, [final_op.id]) == set()

    # Старую выписку можно прислать снова: HOLD не возвращается в книгу.
    repeat = import_rows(session, user.id, [hold, posted], source="Альфа-Банк")
    assert repeat.duplicates == 2
    assert not repeat.new_rows
    assert len(session.scalars(
        select(RawTransaction).where(RawTransaction.user_id == user.id)
    ).all()) == 1


def test_posted_and_pending_in_one_statement_are_both_imported(session, _isolated_db):
    from app.models import User

    user = User(tg_id=5092, display_name="Тест")
    session.add(user)
    session.flush()
    posted = row(when=datetime(2026, 9, 23), amount=-9_500, text="CAFE")
    hold = row(when=datetime(2026, 9, 24), amount=-9_500, text="CAFE")
    hold.raw["pending"] = True
    result = import_rows(session, user.id, [posted, hold], source="Т-Банк")
    assert len(result.new_rows) == 2
    assert result.duplicates == 0


def test_кошелёк_маркетплейса_заводится_отдельным_счётом(session, _isolated_db):
    """Выписка маркетплейса — не карта: иначе пополнение стало бы тратой."""
    from app.ledger.accounts import AccountRegistry
    from app.models import AccountKind, User

    user = User(tg_id=5004, display_name="Кошелёк")
    session.add(user)
    session.flush()

    reg = AccountRegistry(session, user.id)
    from app.ingest.service import resolve_account

    wallet = resolve_account(reg, row(when=datetime(2026, 9, 8, 11, 21), amount=600_000,
                                      text="Пополнение", bank="Wildberries", last4=""))
    card = resolve_account(reg, row(when=datetime(2026, 9, 8, 11, 20), amount=-600_000,
                                    text="Пополнение", bank="Т-Банк"))
    cash = resolve_account(reg, row(when=datetime(2026, 9, 9, 12, 0), amount=-120_000,
                                    text="Рынок", bank="Наличные", last4=""))

    assert wallet.kind == AccountKind.WALLET
    assert card.kind == AccountKind.CARD
    assert cash.kind == AccountKind.CASH


def test_скриншот_без_года_не_улетает_в_прошлый_год():
    """В списке операций банка года нет, и модель подставляет его от себя.

    Промах на год назад — самая молчаливая из возможных поломок: человек
    присылает скриншот этого месяца, ему отвечают «8 операций», а месяц у
    него остаётся пустым. Поэтому дату старше порога, которая при сдвиге
    на год вперёд попадает в недавнее прошлое, считаем промахом модели.
    """
    from datetime import date

    from app.ingest.vision import _parse_date

    today = date(2026, 9, 20)

    # Промах на год: скриншот этого сентября.
    assert _parse_date("2025-09-19", today) == date(2026, 9, 19)
    # День-два вперёд бывают у проводок, это не повод отбрасывать.
    assert _parse_date("2025-09-22", today) == date(2026, 9, 22)
    # Верную дату не трогаем.
    assert _parse_date("2026-09-20", today) == date(2026, 9, 20)
    # Двухмесячную давность тоже: «чек за июль» — обычное дело.
    assert _parse_date("2026-07-15", today) == date(2026, 7, 15)
    # Будущее по-прежнему невозможно.
    assert _parse_date("2027-01-05", today) == date(2026, 1, 5)
    # А честно старую дату двигать бессмысленно: она и после сдвига старая.
    assert _parse_date("2024-03-10", today) == date(2024, 3, 10)
