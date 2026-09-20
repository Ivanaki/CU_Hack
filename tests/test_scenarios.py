"""Семь сценариев кейса. По одному тесту на каждый.

Проверяется не «код не упал», а экономический смысл: сколько из операции
попало в расходы. Именно за это кейс снимает баллы, если ошибиться.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import (
    Claim,
    ClaimKind,
    ClaimStatus,
    Operation,
    OperationKind,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
)


def ops(session, user_id: int, kind: OperationKind) -> list[Operation]:
    return list(
        session.scalars(
            select(Operation)
            .where(Operation.user_id == user_id, Operation.kind == kind)
            .order_by(Operation.occurred_at)
        ).all()
    )


def test_1_перевод_между_своими_картами_не_расход_и_не_доход(session, seeded):
    """10 000 между картами разных банков: в выписках расход и доход, по сути ни то, ни другое."""
    transfers = ops(session, seeded, OperationKind.TRANSFER)
    assert len(transfers) == 1
    transfer = transfers[0]
    assert transfer.amount == 1_000_000
    assert transfer.spend_amount == 0

    # И ни одной проводки в доходы: иначе месяц показал бы фантомные 10 000.
    assert not any(p.account_kind == "income" for p in transfer.postings)


def test_2_пополнение_кошелька_маркетплейса_не_трата_а_траты_с_него_с_категориями(
    session, seeded
):
    """Пополнение WB на 6000 — перевод себе; расход возникает при покупках с кошелька."""
    topups = ops(session, seeded, OperationKind.WALLET_TOPUP)
    assert len(topups) == 1
    assert topups[0].amount == 600_000
    assert topups[0].spend_amount == 0

    # Категории покупок с кошелька восстановлены, а не потеряны.
    from app.models import Account, AccountKind, Posting

    wallet = session.scalar(
        select(Account).where(Account.user_id == seeded, Account.kind == AccountKind.WALLET)
    )
    spend_op_ids = set(
        session.scalars(
            select(Posting.operation_id).where(
                Posting.account_id == wallet.id, Posting.amount < 0
            )
        ).all()
    )
    wallet_spends = [
        op for op in ops(session, seeded, OperationKind.EXPENSE) if op.id in spend_op_ids
    ]
    categories = {op.category_code for op in wallet_spends}
    assert categories == {"groceries", "home"}, categories
    assert sum(op.spend_amount for op in wallet_spends) == 540_000  # 2300 + 3100


def test_3_долг_и_частичный_возврат_с_задержкой(session, seeded):
    """Выдача 3000 и два возврата по 1500 с интервалом в неделю: ничего из этого не расход."""
    lends = ops(session, seeded, OperationKind.DEBT_LEND)
    repays = ops(session, seeded, OperationKind.DEBT_REPAY_IN)
    assert len(lends) == 1
    assert lends[0].amount == 300_000
    assert lends[0].spend_amount == 0
    assert [r.amount for r in repays] == [150_000, 150_000]
    assert all(r.spend_amount == 0 for r in repays)

    claim = session.scalar(
        select(Claim).where(Claim.user_id == seeded, Claim.kind == ClaimKind.DEBT_LENT)
    )
    assert claim.status == ClaimStatus.SETTLED
    assert claim.outstanding == 0


def test_4_счёт_за_компанию_попадает_в_расходы_только_моей_долей(session, seeded):
    """Оплата 8000 за пятерых: реальная трата — одна пятая."""
    bills = ops(session, seeded, OperationKind.SHARED_BILL)
    assert len(bills) == 1
    bill = bills[0]
    assert bill.amount == 800_000
    assert bill.spend_amount == 160_000, "в расходы должна попасть только моя доля"

    reimbursements = ops(session, seeded, OperationKind.REIMBURSEMENT_IN)
    assert len(reimbursements) == 4
    assert all(r.spend_amount == 0 for r in reimbursements), "возврат доли — не доход"

    claim = session.scalar(
        select(Claim).where(Claim.user_id == seeded, Claim.kind == ClaimKind.SHARED_BILL)
    )
    assert claim.participants == 5
    assert claim.my_share == 160_000
    assert claim.status == ClaimStatus.SETTLED


def test_5_поступление_без_зафиксированного_долга_не_угадывается_а_спрашивается(
    session, seeded
):
    """500 «за обед» без долга: система честно говорит, что не знает, и спрашивает."""
    unknowns = ops(session, seeded, OperationKind.UNKNOWN)
    assert len(unknowns) == 1
    unknown = unknowns[0]
    assert unknown.amount == 50_000
    assert unknown.spend_amount == 0

    # Деньги видны, но не объявлены доходом: вторая нога висит на suspense.
    kinds = {p.account_kind for p in unknown.postings}
    assert "suspense" in kinds
    assert "income" not in kinds

    question = session.scalar(
        select(ReviewItem).where(
            ReviewItem.user_id == seeded,
            ReviewItem.kind == ReviewKind.INCOMING_UNKNOWN,
            ReviewItem.status == ReviewStatus.OPEN,
        )
    )
    assert question is not None
    assert len(question.options) == 4, "ответ должен занимать один тап"


def test_6_возврат_товара_не_доход_а_минус_к_трате(session, seeded):
    """4200 вернулись на карту: это отмена покупки, а не заработок."""
    refunds = ops(session, seeded, OperationKind.REFUND)
    assert len(refunds) == 1
    refund = refunds[0]
    assert refund.amount == 420_000
    assert refund.spend_amount == -420_000, "возврат уменьшает расход, а не увеличивает доход"
    assert not any(p.account_kind == "income" for p in refund.postings)
    assert refund.category_code == "electronics"


def test_7_снятие_наличных_становится_тратой_только_когда_известно_на_что(session, seeded):
    """Снятие 5000: сразу не трата. Объяснённые 2000 — трата, неясные 3000 — отдельной строкой."""
    withdrawals = ops(session, seeded, OperationKind.CASH_WITHDRAWAL)
    assert len(withdrawals) == 1
    assert withdrawals[0].amount == 500_000
    assert withdrawals[0].spend_amount == 0

    cash_spends = ops(session, seeded, OperationKind.CASH_EXPENSE)
    assert sum(op.spend_amount for op in cash_spends) == 200_000  # 1200 + 800

    # Остаток, про который никто ничего не сказал, признан тратой по истечении
    # срока — но в отдельной категории, а не размазан по остальным.
    adjustments = [
        op
        for op in ops(session, seeded, OperationKind.ADJUSTMENT)
        if op.category_code == "cash_unallocated"
    ]
    assert sum(op.spend_amount for op in adjustments) == 300_000


def test_ни_один_нейтральный_тип_не_создал_расход_или_доход(session, seeded):
    """Прямая проверка штрафного пункта кейса."""
    from app.ledger.invariants import check_neutral_kinds_have_no_expense

    result = check_neutral_kinds_have_no_expense(session, seeded)
    assert result.ok, result.problems


def test_долг_в_обратную_сторону_не_доход_и_не_расход(session, seeded):
    """Заняли мне 7000, я вернул 7000. Ни то, ни другое не касается расходов.

    Сверх списка кейса, но той же природы: обычный трекер показал бы
    доход 7000 в сентябре и расход 7000 там же, и обе цифры были бы ложью.
    """
    borrowed = ops(session, seeded, OperationKind.DEBT_BORROW)
    repaid = ops(session, seeded, OperationKind.DEBT_REPAY_OUT)

    assert len(borrowed) == 1
    assert borrowed[0].amount == 700_000
    assert borrowed[0].spend_amount == 0
    assert not any(p.account_kind == "income" for p in borrowed[0].postings)

    assert len(repaid) == 1
    assert repaid[0].amount == 700_000
    assert repaid[0].spend_amount == 0, "погашение долга — не трата"

    claim = session.scalar(
        select(Claim).where(Claim.user_id == seeded, Claim.kind == ClaimKind.DEBT_BORROWED)
    )
    assert claim is not None
    assert claim.status == ClaimStatus.SETTLED


def test_ответ_перевод_себе_не_становится_тратой(session, seeded):
    """Штрафной пункт кейса: перевод себе не должен попадать в расходы.

    Даже когда пары в выписках нет и человек сказал об этом сам — деньги
    переезжают на другой его счёт, а не исчезают в «Прочем».
    """
    from datetime import datetime as _dt

    from app.classify.pipeline import DecisionKind, reclassify
    from app.ingest.base import ParsedRow
    from app.ingest.service import import_rows
    from app.models import Decision, OperationSource

    row = ParsedRow(
        booked_at=_dt(2026, 9, 29, 12, 0),
        amount=-500_000,
        description="Перевод Сидоров С. С.",
        counterparty_raw="Сидоров С. С.",
        bank="Т-Банк",
        last4="4127",
    )
    import_rows(session, seeded, [row], source="Т-Банк", origin=OperationSource.IMPORT)
    session.add(
        Decision(
            user_id=seeded,
            anchor_hash=row.dedup_hash(),
            kind=DecisionKind.SET_KIND,
            payload={"kind": "self"},
        )
    )
    session.flush()
    reclassify(session, seeded, now=_dt(2026, 10, 10))

    ops = session.scalars(
        select(Operation).where(
            Operation.user_id == seeded, Operation.rule == "user_decision"
        )
    ).all()
    mine = [o for o in ops if o.amount == 500_000]
    assert mine, "операция не пересоздалась"
    assert all(o.kind == OperationKind.TRANSFER for o in mine)
    assert all(o.spend_amount == 0 for o in mine), "перевод себе не может быть тратой"


def test_пополнение_кошелька_без_выписки_маркетплейса_не_трата(session, seeded):
    """Выписки маркетплейса ещё нет — пополнение всё равно не расход.

    Раньше такое пополнение падало в обычные траты и получало выдуманную
    категорию. Деньги должны ждать на счёте кошелька.
    """
    from datetime import datetime as _dt

    from app.classify.pipeline import reclassify
    from app.ingest.base import ParsedRow
    from app.ingest.service import import_rows
    from app.models import OperationSource

    row = ParsedRow(
        booked_at=_dt(2026, 9, 28, 10, 0),
        amount=-450_000,
        description="Пополнение Ozon Card кошелёк",
        counterparty_raw="OZON CARD",
        bank="Т-Банк",
        last4="4127",
    )
    import_rows(session, seeded, [row], source="Т-Банк", origin=OperationSource.IMPORT)
    reclassify(session, seeded, now=_dt(2026, 10, 10))

    topups = [
        o
        for o in ops(session, seeded, OperationKind.WALLET_TOPUP)
        if o.amount == 450_000
    ]
    assert topups, "пополнение кошелька не распознано"
    assert topups[0].spend_amount == 0, "пополнение кошелька не может быть тратой"
