"""Проверки целостности книги. Гоняются в тестах и по кнопке в боте.

Если хоть одна из них падает, цифрам в аналитике верить нельзя — и лучше
узнать об этом на CI, чем на защите.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AccountKind, Operation, Posting


@dataclass(slots=True)
class CheckResult:
    ok: bool
    problems: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


def check_operations_balanced(session: Session, user_id: int) -> CheckResult:
    """Каждая операция сходится в ноль."""
    rows = session.execute(
        select(Posting.operation_id, func.sum(Posting.amount))
        .where(Posting.user_id == user_id)
        .group_by(Posting.operation_id)
        .having(func.sum(Posting.amount) != 0)
    ).all()
    problems = [f"операция #{op_id}: остаток {total} коп." for op_id, total in rows]
    return CheckResult(not problems, problems)


def check_book_balanced(session: Session, user_id: int) -> CheckResult:
    """Книга в целом сходится в ноль."""
    total = session.scalar(
        select(func.coalesce(func.sum(Posting.amount), 0)).where(Posting.user_id == user_id)
    )
    if total:
        return CheckResult(False, [f"книга не сходится: {total} коп."])
    return CheckResult(True)


def check_no_orphan_postings(session: Session, user_id: int) -> CheckResult:
    """Нет проводок без операции."""
    rows = session.execute(
        select(Posting.id)
        .outerjoin(Operation, Operation.id == Posting.operation_id)
        .where(Posting.user_id == user_id, Operation.id.is_(None))
    ).all()
    problems = [f"проводка #{pid} без операции" for (pid,) in rows]
    return CheckResult(not problems, problems)


def check_neutral_kinds_have_no_expense(session: Session, user_id: int) -> CheckResult:
    """Ни один нейтральный тип не породил расход или доход.

    Это буквально штрафной пункт кейса: "возврат долга или перевод между
    своими счетами отражается доходом или расходом".
    """
    from app.models.enums import _NEUTRAL, OperationKind  # noqa: PLC0415

    neutral = [k.value for k in _NEUTRAL]
    rows = session.execute(
        select(Operation.id, Operation.kind, Posting.account_kind, Posting.amount)
        .join(Posting, Posting.operation_id == Operation.id)
        .where(
            Operation.user_id == user_id,
            Operation.kind.in_(neutral),
            Posting.account_kind.in_([AccountKind.EXPENSE.value, AccountKind.INCOME.value]),
        )
    ).all()
    problems = []
    for op_id, kind, acc_kind, amount in rows:
        # Комиссия внутри перевода — законное исключение и единственное.
        if kind == OperationKind.TRANSFER and acc_kind == AccountKind.EXPENSE:
            continue
        problems.append(
            f"операция #{op_id} типа {kind} создала проводку в {acc_kind} на {amount} коп."
        )
    return CheckResult(not problems, problems)


ALL_CHECKS = (
    check_operations_balanced,
    check_book_balanced,
    check_no_orphan_postings,
    check_neutral_kinds_have_no_expense,
)


def run_all(session: Session, user_id: int) -> CheckResult:
    problems: list[str] = []
    for check in ALL_CHECKS:
        problems.extend(check(session, user_id).problems)
    return CheckResult(not problems, problems)
