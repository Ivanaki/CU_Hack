"""Словарь предметной области. Одно место, где перечислены все виды сущностей."""

from __future__ import annotations

from enum import StrEnum


class AccountKind(StrEnum):
    """Виды счетов в двойной записи.

    Счета делятся на дебетовые (рост = плюс) и кредитовые (рост = минус).
    Инвариант: сумма всех проводок одной операции равна нулю.
    """

    # Активы (дебетовые): реальные и виртуальные хранилища денег
    CARD = "card"  # карта или счёт в банке
    CASH = "cash"  # наличные на руках
    WALLET = "wallet"  # карта/кошелёк маркетплейса (WB, Ozon, Яндекс)
    DEPOSIT = "deposit"  # вклад, накопительный счёт
    RECEIVABLE = "receivable"  # мне должны: выданный долг, доля компании за счёт

    # Обязательства (кредитовые)
    PAYABLE = "payable"  # я должен
    CREDIT = "credit"  # кредитная карта, кредит

    # Результат (дебетовый расход / кредитовый доход)
    EXPENSE = "expense"  # категория расхода
    INCOME = "income"  # категория дохода

    # Технический: сюда встаёт вторая нога операции, смысл которой ещё не понят.
    # Ненулевой остаток на suspense — это ровно те деньги, про которые
    # инструмент честно говорит "я не знаю, что это".
    SUSPENSE = "suspense"

    @property
    def is_asset(self) -> bool:
        return self in _ASSETS

    @property
    def is_own_money(self) -> bool:
        """Реальные деньги пользователя: из них складывается 'сколько у меня есть'."""
        return self in _OWN_MONEY


_ASSETS = frozenset(
    {AccountKind.CARD, AccountKind.CASH, AccountKind.WALLET, AccountKind.DEPOSIT,
     AccountKind.RECEIVABLE}
)
_OWN_MONEY = frozenset(
    {AccountKind.CARD, AccountKind.CASH, AccountKind.WALLET, AccountKind.DEPOSIT}
)


class OperationKind(StrEnum):
    """Модель денег: полный список типов экономических событий.

    Каждый тип отвечает на вопрос "уменьшилось ли моё богатство и на сколько".
    Именно это, а не знак в выписке, определяет попадание в расходы.
    """

    EXPENSE = "expense"  # обычная трата
    INCOME = "income"  # обычный доход
    TRANSFER = "transfer"  # перевод между своими счетами: ни расход, ни доход
    WALLET_TOPUP = "wallet_topup"  # пополнение кошелька маркетплейса
    CASH_WITHDRAWAL = "cash_withdrawal"  # снятие наличных: перемещение, не трата
    CASH_EXPENSE = "cash_expense"  # трата наличными (заносится вручную/голосом)

    DEBT_LEND = "debt_lend"  # я дал в долг
    DEBT_REPAY_IN = "debt_repay_in"  # мне вернули долг
    DEBT_BORROW = "debt_borrow"  # я занял
    DEBT_REPAY_OUT = "debt_repay_out"  # я вернул

    SHARED_BILL = "shared_bill"  # заплатил за компанию, вернут часть
    REIMBURSEMENT_IN = "reimbursement_in"  # компенсация доли по общему счёту

    REFUND = "refund"  # возврат товара: минус к прошлой трате, не доход

    ADJUSTMENT = "adjustment"  # служебная корректировка (списание невозврата и т.п.)
    UNKNOWN = "unknown"  # смысл не установлен, висит на suspense

    @property
    def is_neutral(self) -> bool:
        """Типы, которые вообще не должны попадать ни в расходы, ни в доходы."""
        return self in _NEUTRAL


_NEUTRAL = frozenset(
    {
        OperationKind.TRANSFER,
        OperationKind.WALLET_TOPUP,
        OperationKind.CASH_WITHDRAWAL,
        OperationKind.DEBT_LEND,
        OperationKind.DEBT_REPAY_IN,
        OperationKind.DEBT_BORROW,
        OperationKind.DEBT_REPAY_OUT,
        OperationKind.REIMBURSEMENT_IN,
    }
)


class OperationStatus(StrEnum):
    AUTO = "auto"  # классифицировано правилом с высокой уверенностью
    NEEDS_REVIEW = "needs_review"  # висит вопрос пользователю
    CONFIRMED = "confirmed"  # пользователь подтвердил одним тапом
    CORRECTED = "corrected"  # пользователь исправил классификацию


class OperationSource(StrEnum):
    IMPORT = "import"  # файл выписки — основной источник
    SCREENSHOT = "screenshot"  # скриншот из приложения банка
    RECEIPT = "receipt"  # фото чека
    MANUAL = "manual"  # быстрый ручной ввод
    VOICE = "voice"  # голосовое сообщение
    SYSTEM = "system"  # сгенерировано системой (списание невозврата, наличные)

    @property
    def is_statement(self) -> bool:
        """Выписка — полный и точный источник; остальное вводится по ходу дня.

        Различие нужно дедупликации: одна и та же покупка, попавшая и в
        скриншот, и позже в выписку, — это один факт, а не два.
        """
        return self is OperationSource.IMPORT


class ClaimKind(StrEnum):
    DEBT_LENT = "debt_lent"  # я дал в долг
    DEBT_BORROWED = "debt_borrowed"  # я занял
    SHARED_BILL = "shared_bill"  # заплатил за компанию


class ClaimStatus(StrEnum):
    OPEN = "open"
    SETTLED = "settled"
    WRITTEN_OFF = "written_off"  # срок вышел, остаток признан тратой


class CounterpartyKind(StrEnum):
    PERSON = "person"  # физлицо: долги, общие счета
    MERCHANT = "merchant"  # магазин, сервис
    SELF = "self"  # я сам (переводы между своими счетами)
    BANK = "bank"  # банк: комиссии, кэшбэк, проценты
    UNKNOWN = "unknown"


class ReviewKind(StrEnum):
    """Виды вопросов пользователю. Каждый закрывается одним тапом."""

    INCOMING_UNKNOWN = "incoming_unknown"  # 500 "за обед" без зафиксированного долга
    SHARED_BILL_HEADS = "shared_bill_heads"  # за скольких человек заплатил
    IS_SELF_TRANSFER = "is_self_transfer"  # похоже на перевод себе, но неточно
    CASH_SPENT_ON = "cash_spent_on"  # на что ушли снятые наличные
    CATEGORY_UNCLEAR = "category_unclear"  # трата без понятной категории
    CONFIRM_SUBSCRIPTION = "confirm_subscription"  # найдено регулярное списание
    OUTGOING_TO_PERSON = "outgoing_to_person"  # перевод человеку: долг, подарок, оплата
    LARGE_UNCATEGORIZED = "large_uncategorized"  # крупная трата без категории
    UNKNOWN_PLACE = "unknown_place"  # неопознанное место: один вопрос на все траты в нём


class ReviewStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    SKIPPED = "skipped"


class MatchType(StrEnum):
    EXACT = "exact"
    CONTAINS = "contains"
    REGEX = "regex"
    MCC = "mcc"
