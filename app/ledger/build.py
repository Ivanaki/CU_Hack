"""Построение проводок для каждого типа операции.

Здесь в одном месте собран ответ на главный вопрос кейса: какие движения
денег являются тратой, а какие нет. Каждая функция читается как определение.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.classify import categories as cats
from app.ledger.accounts import AccountRegistry
from app.ledger.writer import Entry, post
from app.models import Account, Money, Operation


def expense(
    session: Session,
    reg: AccountRegistry,
    op: Operation,
    *,
    account: Account,
    amount: Money,
    category: str | None,
) -> None:
    """Обычная трата: деньги ушли со счёта в категорию расхода."""
    post(session, op, [
        Entry(account, -amount, memo=op.title),
        Entry(reg.expense(category), amount, memo=op.title),
    ])


def income(
    session: Session,
    reg: AccountRegistry,
    op: Operation,
    *,
    account: Account,
    amount: Money,
    category: str | None,
) -> None:
    """Обычный доход."""
    post(session, op, [
        Entry(account, amount, memo=op.title),
        Entry(reg.income(category), -amount, memo=op.title),
    ])


def transfer(
    session: Session,
    reg: AccountRegistry,
    op: Operation,
    *,
    src: Account,
    dst: Account,
    sent: Money,
    received: Money,
) -> None:
    """Перевод между своими счетами. Ни расход, ни доход.

    Расхождение сумм — это комиссия банка, и она единственная здесь является
    настоящей тратой. Сценарий 1 кейса: 10 000 между своими картами разных
    банков перестаёт быть одновременно расходом и доходом.
    """
    entries = [Entry(src, -sent, memo=op.title), Entry(dst, received, memo=op.title)]
    fee = sent - received
    if fee > 0:
        entries.append(Entry(reg.expense("fees"), fee, memo=f"Комиссия: {op.title}"))
    elif fee < 0:
        # Пришло больше, чем ушло: начисление процентов внутри перевода.
        entries.append(Entry(reg.income("interest"), fee, memo=f"Начисление: {op.title}"))
    post(session, op, entries)


def wallet_topup(
    session: Session,
    reg: AccountRegistry,
    op: Operation,
    *,
    src: Account,
    wallet: Account,
    amount: Money,
) -> None:
    """Пополнение карты маркетплейса. Сценарий 2 кейса.

    Деньги переехали на другой мой счёт. Расход появится позже, когда с
    этого кошелька что-то купят — и уже с правильной категорией.
    """
    post(session, op, [
        Entry(src, -amount, memo=op.title),
        Entry(wallet, amount, memo=op.title),
    ])


def cash_withdrawal(
    session: Session,
    reg: AccountRegistry,
    op: Operation,
    *,
    account: Account,
    amount: Money,
) -> None:
    """Снятие наличных. Сценарий 7 кейса.

    Наш ответ на вопрос "считать тратой сразу, позже или разделить":
    в момент снятия это перемещение между своими активами. Тратой каждый
    рубль становится, когда пользователь скажет, на что он ушёл. Остаток
    висит на счёте наличных и виден отдельной строкой — он честно
    показывает, сколько денег ещё не объяснено.
    """
    post(session, op, [
        Entry(account, -amount, memo=op.title),
        Entry(reg.cash(), amount, memo=op.title),
    ])


def cash_expense(
    session: Session,
    reg: AccountRegistry,
    op: Operation,
    *,
    amount: Money,
    category: str | None,
) -> None:
    """Трата наличными: единственное, что действительно вводится руками."""
    post(session, op, [
        Entry(reg.cash(), -amount, memo=op.title),
        Entry(reg.expense(category), amount, memo=op.title),
    ])


def debt_lend(
    session: Session,
    reg: AccountRegistry,
    op: Operation,
    *,
    account: Account,
    receivable: Account,
    amount: Money,
) -> None:
    """Выдача долга. Сценарий 3 кейса: не расход, а смена формы актива."""
    post(session, op, [
        Entry(account, -amount, memo=op.title),
        Entry(receivable, amount, memo=op.title),
    ])


def debt_repay_in(
    session: Session,
    reg: AccountRegistry,
    op: Operation,
    *,
    account: Account,
    receivable: Account,
    amount: Money,
) -> None:
    """Возврат долга мне. Не доход. Работает и для частичных возвратов."""
    post(session, op, [
        Entry(account, amount, memo=op.title),
        Entry(receivable, -amount, memo=op.title),
    ])


def debt_borrow(
    session: Session,
    reg: AccountRegistry,
    op: Operation,
    *,
    account: Account,
    payable: Account,
    amount: Money,
) -> None:
    """Я занял: деньги пришли, но это не доход, а обязательство."""
    post(session, op, [
        Entry(account, amount, memo=op.title),
        Entry(payable, -amount, memo=op.title),
    ])


def debt_repay_out(
    session: Session,
    reg: AccountRegistry,
    op: Operation,
    *,
    account: Account,
    payable: Account,
    amount: Money,
) -> None:
    """Я вернул: не расход, гашение обязательства."""
    post(session, op, [
        Entry(account, -amount, memo=op.title),
        Entry(payable, amount, memo=op.title),
    ])


def shared_bill(
    session: Session,
    reg: AccountRegistry,
    op: Operation,
    *,
    account: Account,
    receivable: Account,
    total: Money,
    my_share: Money,
    category: str | None,
) -> None:
    """Оплата за компанию. Сценарий 4 кейса.

    Со счёта ушло 8000, но моя трата — 1600. Остальные 6400 сразу
    становятся дебиторкой, а не расходом. Поэтому месяц показывает
    честные 1600, а неделя не проваливается в минус, когда друзья
    возвращают деньги.
    """
    others = total - my_share
    post(session, op, [
        Entry(account, -total, memo=op.title),
        Entry(reg.expense(category), my_share, memo=f"Моя доля: {op.title}"),
        Entry(receivable, others, memo=f"Доля компании: {op.title}"),
    ])


def reimbursement_in(
    session: Session,
    reg: AccountRegistry,
    op: Operation,
    *,
    account: Account,
    receivable: Account,
    amount: Money,
) -> None:
    """Друг вернул свою долю. Ни доход, ни отрицательный расход — гашение."""
    post(session, op, [
        Entry(account, amount, memo=op.title),
        Entry(receivable, -amount, memo=op.title),
    ])


def claim_writeoff(
    session: Session,
    reg: AccountRegistry,
    op: Operation,
    *,
    receivable: Account,
    amount: Money,
    category: str | None,
    at: datetime,
) -> None:
    """Срок вышел, деньги не вернули: непогашенный остаток признаётся тратой.

    Датой дедлайна, а не датой исходного события — чтобы уже закрытые
    периоды не переписывались задним числом.
    """
    post(session, op, [
        Entry(receivable, -amount, at=at, memo=op.title),
        Entry(reg.expense(category), amount, at=at, memo=op.title),
    ])


def refund(
    session: Session,
    reg: AccountRegistry,
    op: Operation,
    *,
    account: Account,
    amount: Money,
    category: str | None,
    at: datetime | None = None,
) -> None:
    """Возврат товара. Сценарий 6 кейса: не доход, а минус к трате.

    Проводка датируется днём возврата, а не днём покупки: закрытые периоды
    остаются неизменными, а сумма за больший период всё равно сходится.
    """
    post(session, op, [
        Entry(account, amount, at=at, memo=op.title),
        Entry(reg.expense(category), -amount, at=at, memo=op.title),
    ])


def unknown(
    session: Session,
    reg: AccountRegistry,
    op: Operation,
    *,
    account: Account,
    amount: Money,
) -> None:
    """Смысл не установлен. Сценарий 5 кейса: 500 "за обед" без долга.

    Деньги видны на счёте, но не объявлены ни доходом, ни возвратом.
    Вторая нога висит на suspense, пока пользователь не ответит одним тапом.
    Так незнание системы становится видимой величиной, а не тихой ошибкой
    в цифре расходов.
    """
    post(session, op, [
        Entry(account, amount, memo=op.title),
        Entry(reg.suspense(), -amount, memo=op.title),
    ])


def unallocated_cash(
    session: Session,
    reg: AccountRegistry,
    op: Operation,
    *,
    amount: Money,
    at: datetime,
) -> None:
    """Наличные, про которые пользователь так и не рассказал.

    Через настроенный срок остаток переводится в отдельную категорию
    "Наличные (не распределены)": деньги потрачены, это факт, но на что —
    неизвестно, и отчёт говорит об этом прямо.
    """
    post(session, op, [
        Entry(reg.cash(), -amount, at=at, memo=op.title),
        Entry(reg.expense(cats.CASH_UNALLOCATED), amount, at=at, memo=op.title),
    ])
