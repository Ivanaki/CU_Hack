"""Реестр счетов. Заводит счета по требованию и кеширует их в рамках сессии.

Счета делятся на реальные (карты, наличные, кошельки маркетплейсов) и
виртуальные (категории расходов и доходов, дебиторка по каждому долгу,
suspense). Виртуальные нужны только для того, чтобы каждая операция
сходилась в ноль.
"""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classify import categories as cats
from app.models import Account, AccountKind, Counterparty, CounterpartyKind

SUSPENSE_KEY = "suspense:unresolved"
CASH_KEY = "cash:wallet"


def norm_key(text: str) -> str:
    """Нормализует имя контрагента для сопоставления.

    Иван П. / ИВАН П / Иван П -> иван п. Латиница и кириллица не смешиваются,
    ё приводится к е: в выписках разных банков одно и то же имя пишется
    по-разному, и без этого один человек превращается в трёх.
    """
    text = unicodedata.normalize("NFKC", text or "").casefold().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class AccountRegistry:
    """Get-or-create для счетов. Один экземпляр на единицу работы."""

    def __init__(self, session: Session, user_id: int) -> None:
        self.session = session
        self.user_id = user_id
        self._cache: dict[str, Account] = {}

    # -- базовый доступ ----------------------------------------------------

    def get_or_create(
        self,
        *,
        kind: AccountKind,
        key: str,
        name: str,
        bank: str | None = None,
        last4: str | None = None,
        category_code: str | None = None,
        counterparty_id: int | None = None,
        currency: str = "RUB",
    ) -> Account:
        if key in self._cache:
            return self._cache[key]
        acc = self.session.scalar(
            select(Account).where(Account.user_id == self.user_id, Account.key == key)
        )
        if acc is None:
            acc = Account(
                user_id=self.user_id,
                kind=kind,
                key=key,
                name=name,
                bank=bank,
                last4=last4,
                category_code=category_code,
                counterparty_id=counterparty_id,
                currency=currency,
            )
            self.session.add(acc)
            self.session.flush()
        self._cache[key] = acc
        return acc

    def by_key(self, key: str) -> Account | None:
        if key in self._cache:
            return self._cache[key]
        acc = self.session.scalar(
            select(Account).where(Account.user_id == self.user_id, Account.key == key)
        )
        if acc is not None:
            self._cache[key] = acc
        return acc

    # -- типовые счета -----------------------------------------------------

    def bank_account(
        self, bank: str, last4: str | None, *, name: str | None = None,
        kind: AccountKind = AccountKind.CARD,
    ) -> Account:
        suffix = last4 or "main"
        key = f"{norm_key(bank).replace(' ', '')}:{suffix}"
        pretty = name or (f"{bank} ·{last4}" if last4 else bank)
        return self.get_or_create(kind=kind, key=key, name=pretty, bank=bank, last4=last4)

    def wallet_account(self, merchant: str) -> Account:
        """Кошелёк маркетплейса — отдельный актив, а не категория трат."""
        key = f"wallet:{norm_key(merchant).replace(' ', '')}"
        return self.get_or_create(
            kind=AccountKind.WALLET, key=key, name=f"Кошелёк {merchant}", bank=merchant
        )

    def cash(self) -> Account:
        return self.get_or_create(kind=AccountKind.CASH, key=CASH_KEY, name="Наличные")

    def expense(self, category_code: str | None) -> Account:
        code = category_code or cats.OTHER
        return self.get_or_create(
            kind=AccountKind.EXPENSE,
            key=f"expense:{code}",
            name=cats.title(code),
            category_code=code,
        )

    def income(self, category_code: str | None) -> Account:
        code = category_code or cats.OTHER_INCOME
        return self.get_or_create(
            kind=AccountKind.INCOME,
            key=f"income:{code}",
            name=cats.title(code),
            category_code=code,
        )

    def receivable(self, slug: str, name: str, counterparty_id: int | None = None) -> Account:
        return self.get_or_create(
            kind=AccountKind.RECEIVABLE,
            key=f"receivable:{slug}",
            name=name,
            counterparty_id=counterparty_id,
        )

    def payable(self, slug: str, name: str, counterparty_id: int | None = None) -> Account:
        return self.get_or_create(
            kind=AccountKind.PAYABLE,
            key=f"payable:{slug}",
            name=name,
            counterparty_id=counterparty_id,
        )

    def suspense(self) -> Account:
        """Счёт нераспознанного. Его ненулевой остаток — честная метрика незнания."""
        return self.get_or_create(
            kind=AccountKind.SUSPENSE, key=SUSPENSE_KEY, name="Не разобрано"
        )

    # -- контрагенты -------------------------------------------------------

    def counterparty(
        self, display_name: str, kind: CounterpartyKind = CounterpartyKind.UNKNOWN
    ) -> Counterparty:
        key = norm_key(display_name)
        cp = self.session.scalar(
            select(Counterparty).where(
                Counterparty.user_id == self.user_id, Counterparty.norm_key == key
            )
        )
        if cp is None:
            cp = Counterparty(
                user_id=self.user_id, kind=kind, display_name=display_name.strip(), norm_key=key
            )
            self.session.add(cp)
            self.session.flush()
        elif cp.kind == CounterpartyKind.UNKNOWN and kind != CounterpartyKind.UNKNOWN:
            cp.kind = kind
        return cp
