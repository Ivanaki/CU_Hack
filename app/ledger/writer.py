"""Запись проводок. Единственный способ, которым деньги попадают в аналитику.

Соглашение о знаках — дебет положительный:

    активы и расходы   + = увеличение
    доходы и пассивы   + = уменьшение (доход пишется минусом)

Из этого следует один-единственный инвариант: сумма проводок операции = 0.
Он проверяется здесь, при записи. Нарушить его и записать кривые цифры в
базу технически невозможно.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import Account, Money, Operation, Posting


class UnbalancedOperation(ValueError):
    """Проводки операции не сходятся в ноль."""


@dataclass(slots=True)
class Entry:
    """Одна нога проводки.

    at=None означает "датой операции". Явная дата нужна там, где
    экономический смысл возникает позже самого платежа: списание
    невозвращённой доли по общему счёту датируется дедлайном, а не ужином.
    """

    account: Account
    amount: Money
    at: datetime | None = None
    memo: str = ""


def post(session: Session, operation: Operation, entries: list[Entry]) -> list[Posting]:
    """Перезаписывает проводки операции. Идемпотентно."""
    entries = [e for e in entries if e.amount != 0]
    total = sum(e.amount for e in entries)
    if total != 0:
        detail = ", ".join(f"{e.account.key}={e.amount}" for e in entries)
        raise UnbalancedOperation(
            f"операция #{operation.id} ({operation.kind}) не сходится: "
            f"остаток {total} коп. при проводках [{detail}]"
        )

    if operation.id is not None:
        session.execute(delete(Posting).where(Posting.operation_id == operation.id))

    created: list[Posting] = []
    for entry in entries:
        posting = Posting(
            user_id=operation.user_id,
            operation_id=operation.id,
            account_id=entry.account.id,
            at=entry.at or operation.occurred_at,
            amount=entry.amount,
            account_kind=entry.account.kind,
            category_code=entry.account.category_code,
            memo=entry.memo or operation.title,
        )
        session.add(posting)
        created.append(posting)
    session.flush()

    # spend_amount — денормализация для UI. Считаем её из тех же проводок,
    # чтобы карточка операции и отчёт не могли разойтись.
    operation.spend_amount = sum(
        p.amount for p in created if p.account_kind == "expense"
    )
    return created


def clear(session: Session, operation: Operation) -> None:
    if operation.id is not None:
        session.execute(delete(Posting).where(Posting.operation_id == operation.id))
    operation.spend_amount = 0
