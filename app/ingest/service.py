"""Загрузка операций в базу: строки -> RawTransaction, с дедупликацией.

Дедупликация двухуровневая.

Точная — по хешу: повторная загрузка той же выписки ничего не портит.

Мягкая — по смыслу: одна и та же покупка, присланная сегодня скриншотом и
приехавшая через месяц в выписке, это один факт. Совпадение по счёту,
сумме, дню и названию продавца считается совпадением, и выигрывает выписка
как более точный источник; ответы пользователя при этом переносятся на
уцелевшую запись, чтобы вопрос не пришлось отвечать дважды.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classify.categories import WALLET_MERCHANTS
from app.ingest.base import ParsedRow, parse_statement
from app.ledger.accounts import AccountRegistry, norm_key
from app.models import (
    Account,
    AccountKind,
    Decision,
    ImportBatch,
    OperationSource,
    RawTransaction,
    ReviewItem,
)

SOFT_WINDOW = timedelta(hours=36)


@dataclass(slots=True)
class ImportResult:
    batch: ImportBatch
    new_rows: list[RawTransaction]
    duplicates: int
    superseded: int = 0

    @property
    def summary(self) -> str:
        tail = f", уточнено {self.superseded}" if self.superseded else ""
        return (
            f"{self.batch.source}: новых {len(self.new_rows)}, "
            f"дублей {self.duplicates}{tail} из {self.batch.rows_total}"
        )


def _is_wallet_bank(bank: str) -> bool:
    key = norm_key(bank)
    return any(norm_key(m) in key or key in norm_key(m) for m in WALLET_MERCHANTS)


# Источники, которые на самом деле описывают наличные, а не счёт в банке.
CASH_SOURCES = ("наличные", "нал", "cash", "вручную", "кошелек", "кошелёк")


def _is_cash_source(bank: str) -> bool:
    return norm_key(bank) in {norm_key(name) for name in CASH_SOURCES}


def resolve_account(reg: AccountRegistry, row: ParsedRow) -> Account:
    """Счёт, к которому относится строка выписки.

    Выписка маркетплейса заводится не как карта, а как кошелёк: это меняет
    смысл пополнения с траты на перевод и позволяет связать пополнение с
    последующими покупками.
    """
    if _is_cash_source(row.bank):
        # Траты наличными списываются с кошелька наличных, иначе снятие в
        # банкомате никогда бы не закрывалось и висело бы вечно.
        return reg.cash()
    if _is_wallet_bank(row.bank):
        return reg.wallet_account(row.bank)
    return reg.bank_account(row.bank, row.last4, name=row.account_name)


def _similar_merchant(a: str, b: str) -> bool:
    """Грубое сходство названий: скриншот и выписка пишут продавца по-разному."""
    x, y = norm_key(a), norm_key(b)
    if not x or not y:
        return True  # нечего сравнивать — полагаемся на сумму, день и счёт
    if x in y or y in x:
        return True
    tokens_x = {t for t in x.split() if len(t) > 2}
    tokens_y = {t for t in y.split() if len(t) > 2}
    if not tokens_x or not tokens_y:
        return False
    overlap = len(tokens_x & tokens_y) / min(len(tokens_x), len(tokens_y))
    return overlap >= 0.5


def _find_soft_duplicate(
    session: Session,
    user_id: int,
    row: ParsedRow,
    account_id: int,
    source: OperationSource,
    *, consumed: set[int],
) -> RawTransaction | None:
    """Ищет тот же факт, пришедший из источника другого типа."""
    candidates = session.scalars(
        select(RawTransaction).where(
            RawTransaction.user_id == user_id,
            RawTransaction.account_id == account_id,
            RawTransaction.amount == row.amount,
            RawTransaction.booked_at >= row.booked_at - SOFT_WINDOW,
            RawTransaction.booked_at <= row.booked_at + SOFT_WINDOW,
        )
    ).all()
    for candidate in candidates:
        if candidate.id in consumed:
            continue
        # Совпадение внутри одного типа источника — это две разные покупки
        # (два кофе за день), а не дубль.
        if OperationSource(candidate.source).is_statement == source.is_statement:
            continue
        if _similar_merchant(candidate.counterparty_raw or candidate.description,
                             row.counterparty_raw or row.description):
            return candidate
    return None


def _move_user_answers(session: Session, user_id: int, old_hash: str, new_hash: str) -> None:
    """Переносит решения пользователя на уцелевшую запись."""
    for decision in session.scalars(
        select(Decision).where(Decision.user_id == user_id, Decision.anchor_hash == old_hash)
    ).all():
        clash = session.scalar(
            select(Decision).where(
                Decision.user_id == user_id,
                Decision.anchor_hash == new_hash,
                Decision.kind == decision.kind,
            )
        )
        if clash is None:
            decision.anchor_hash = new_hash
        else:
            session.delete(decision)
    for item in session.scalars(
        select(ReviewItem).where(ReviewItem.user_id == user_id, ReviewItem.anchor_hash == old_hash)
    ).all():
        item.anchor_hash = new_hash


def import_rows(
    session: Session,
    user_id: int,
    rows: list[ParsedRow],
    *,
    source: str,
    filename: str = "",
    origin: OperationSource = OperationSource.IMPORT,
) -> ImportResult:
    reg = AccountRegistry(session, user_id)
    batch = ImportBatch(
        user_id=user_id,
        source=source,
        filename=filename,
        rows_total=len(rows),
        period_from=min((r.booked_at.date() for r in rows), default=None),
        period_to=max((r.booked_at.date() for r in rows), default=None),
    )
    session.add(batch)
    session.flush()

    existing_hashes: set[str] = set(
        session.scalars(
            select(RawTransaction.dedup_hash).where(RawTransaction.user_id == user_id)
        ).all()
    )

    created: list[RawTransaction] = []
    duplicates = 0
    superseded = 0
    consumed: set[int] = set()

    for row in rows:
        digest = row.dedup_hash()
        if digest in existing_hashes:
            duplicates += 1
            continue

        account = resolve_account(reg, row)
        twin = _find_soft_duplicate(
            session, user_id, row, account.id, origin, consumed=consumed
        )
        if twin is not None:
            consumed.add(twin.id)
            if not origin.is_statement:
                # Приехало из быстрого источника, а выписка уже есть — она точнее.
                duplicates += 1
                continue
            # Выписка уточняет то, что раньше пришло скриншотом.
            _move_user_answers(session, user_id, twin.dedup_hash, digest)
            session.delete(twin)
            session.flush()
            superseded += 1

        existing_hashes.add(digest)
        raw = RawTransaction(
            user_id=user_id,
            batch_id=batch.id,
            account_id=account.id,
            booked_at=row.booked_at,
            amount=row.amount,
            currency=row.currency,
            description=row.description,
            mcc=row.mcc,
            counterparty_raw=row.counterparty_raw or row.description,
            external_id=row.external_id,
            dedup_hash=digest,
            source=origin,
        )
        session.add(raw)
        created.append(raw)

    batch.rows_new = len(created)
    batch.rows_duplicate = duplicates
    session.flush()
    return ImportResult(
        batch=batch, new_rows=created, duplicates=duplicates, superseded=superseded
    )


def import_file(
    session: Session, user_id: int, data: bytes, *, filename: str = ""
) -> ImportResult:
    bank, rows = parse_statement(data, filename=filename)
    return import_rows(
        session, user_id, rows, source=bank, filename=filename,
        origin=OperationSource.IMPORT,
    )


def import_images(
    session: Session, user_id: int, images: list[bytes], **kwargs
) -> tuple[ImportResult, str]:
    """Картинка от пользователя: скриншот банка или фото чека.

    Вид определяет модель, а не пользователь: просить выбрать в меню
    «это чек или это скриншот» — лишний шаг там, где можно обойтись без него.
    """
    from app.ingest.vision import parse_images  # noqa: PLC0415

    result = parse_images(images, **kwargs)
    origin = (
        OperationSource.RECEIPT if result.kind == "receipt" else OperationSource.SCREENSHOT
    )
    source = result.source_name or "Картинка"
    return (
        import_rows(
            session, user_id, result.rows, source=source,
            filename=result.kind, origin=origin,
        ),
        result.kind,
    )


def import_screenshot(
    session: Session, user_id: int, images: list[bytes], **kwargs
) -> ImportResult:
    """Совместимость: только результат импорта, без вида картинки."""
    return import_images(session, user_id, images, **kwargs)[0]


def add_manual(
    session: Session,
    user_id: int,
    rows: list[ParsedRow],
    *,
    origin: OperationSource = OperationSource.MANUAL,
) -> ImportResult:
    """Быстрый ручной или голосовой ввод — в основном наличные."""
    return import_rows(
        session, user_id, rows, source="Вручную", filename="", origin=origin
    )


def pending_raw(session: Session, user_id: int) -> list[RawTransaction]:
    """Сырые строки, ещё не собранные ни в одну операцию."""
    return list(
        session.scalars(
            select(RawTransaction)
            .where(RawTransaction.user_id == user_id, RawTransaction.operation_id.is_(None))
            .order_by(RawTransaction.booked_at, RawTransaction.id)
        ).all()
    )


def own_account_ids(session: Session, user_id: int) -> set[int]:
    """Счета, между которыми перевод не является ни расходом, ни доходом."""
    kinds = [
        AccountKind.CARD.value,
        AccountKind.CASH.value,
        AccountKind.WALLET.value,
        AccountKind.DEPOSIT.value,
    ]
    return set(
        session.scalars(
            select(Account.id).where(Account.user_id == user_id, Account.kind.in_(kinds))
        ).all()
    )
