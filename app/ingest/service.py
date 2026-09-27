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

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classify.categories import WALLET_MERCHANTS
from app.ingest.base import ParsedRow, parse_statement
from app.ingest.vision import canonical_bank
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
PENDING_WINDOW = timedelta(days=14)


@dataclass(slots=True)
class ImportResult:
    batch: ImportBatch
    new_rows: list[RawTransaction]
    duplicates: int
    superseded: int = 0
    extraction_method: str = "csv"
    pending_count: int = 0

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
    if row.raw.get("vision") and not row.last4 and canonical_bank(row.bank):
        # Скрин не показывает номер карты. Если банк представлен единственным
        # известным счётом, относим новые покупки к нему, а не к "bank:main".
        known = [account for account in reg.session.scalars(
            select(Account).where(
                Account.user_id == reg.user_id,
                Account.kind == AccountKind.CARD,
                Account.last4.is_not(None),
            )
        ).all() if canonical_bank(account.bank) == canonical_bank(row.bank)]
        if len(known) == 1:
            return known[0]
    return reg.bank_account(row.bank, row.last4, name=row.account_name)


def _similar_merchant(a: str, b: str) -> bool:
    """Грубое сходство названий: скриншот и выписка пишут продавца по-разному."""
    x, y = _merchant_key(a), _merchant_key(b)
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


_MERCHANT_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("метро москвы", ("lk metro", "метро москвы", "московский метрополитен")),
    ("пятерочка", ("pyaterochka", "пятерочка")),
    ("неофарм", ("neo farm", "неофарм")),
    ("ригла", ("mskapt", "ригла")),
    ("ggsel", ("guuduu", "гуудуу", "ggsel")),
)


def _merchant_key(value: str) -> str:
    key = norm_key(value)
    for canonical, aliases in _MERCHANT_ALIASES:
        if any(alias in key for alias in aliases):
            return canonical
    return key


def _find_soft_duplicate(
    session: Session,
    user_id: int,
    row: ParsedRow,
    account_id: int,
    source: OperationSource,
    *, consumed: set[int],
) -> list[RawTransaction]:
    """Ищет тот же факт из другого источника, даже без номера карты на скрине.

    Счёт без последних цифр не является доказательством другого счёта. При
    разных названиях банков разрешаем только единственное совпадение покупки
    по дню, точной сумме и продавцу; перевод с общим названием не склеиваем.
    """
    candidates = session.scalars(
        select(RawTransaction).where(
            RawTransaction.user_id == user_id,
            RawTransaction.amount >= row.amount - 1_000,
            RawTransaction.amount <= row.amount + 1_000,
            RawTransaction.booked_at >= row.booked_at - SOFT_WINDOW,
            RawTransaction.booked_at <= row.booked_at + SOFT_WINDOW,
        )
    ).all()
    matches: list[RawTransaction] = []
    near_matches: list[RawTransaction] = []
    transfer_matches: list[RawTransaction] = []
    for candidate in candidates:
        if candidate.id in consumed:
            continue
        # Совпадение внутри одного типа источника — это две разные покупки
        # (два кофе за день), а не дубль.
        if OperationSource(candidate.source).is_statement == source.is_statement:
            continue
        if (candidate.amount > 0) != (row.amount > 0):
            continue
        candidate_name = candidate.counterparty_raw or candidate.description
        row_name = row.counterparty_raw or row.description
        merchant_matches = _similar_merchant(candidate_name, row_name)
        same_bank = candidate.account_id == account_id
        if candidate.account_id != account_id:
            account = candidate.account
            same_bank = canonical_bank(account.bank) == canonical_bank(row.bank)
            if same_bank:
                # Две известные разные карты банка — разные источники денег.
                if account.last4 and row.last4 and account.last4 != row.last4:
                    continue
            else:
                # Неверно распознанный банк исправляет только точное,
                # однозначное совпадение покупки, а не общий текст перевода.
                if (
                    source != OperationSource.SCREENSHOT
                    and candidate.source != OperationSource.SCREENSHOT
                ):
                    continue
                if row.amount > 0 or not _specific_merchant_match(candidate_name, row_name):
                    continue
                if account.last4 and row.last4:
                    continue
        if not merchant_matches:
            if (
                same_bank
                and candidate.amount == row.amount
                and candidate.booked_at.date() == row.booked_at.date()
                and _transfer_like(candidate_name)
                and _transfer_like(row_name)
            ):
                transfer_matches.append(candidate)
            continue
        if candidate.amount == row.amount:
            matches.append(candidate)
        elif (
            same_bank
            and row.booked_at.date() == candidate.booked_at.date()
            and abs(candidate.amount - row.amount) <= min(1_000, abs(row.amount) // 50)
            and _specific_merchant_match(candidate_name, row_name)
        ):
            # OCR иногда меняет одну цифру суммы. Только однозначный
            # близкий платёж на том же счёте можно заменить точной выпиской.
            near_matches.append(candidate)
    if len(matches) == 1:
        if (
            source.is_statement
            and near_matches
            and all(candidate.source == OperationSource.SCREENSHOT for candidate in near_matches)
            and all(candidate.account_id == matches[0].account_id for candidate in near_matches)
            and all(_merchant_key(candidate.counterparty_raw or candidate.description)
                    == _merchant_key(matches[0].counterparty_raw or matches[0].description)
                    for candidate in near_matches)
        ):
            return matches + near_matches
        return matches
    if (
        len(matches) > 1
        and source.is_statement
        and all(candidate.source == OperationSource.SCREENSHOT for candidate in matches)
        and len({candidate.account_id for candidate in matches}) == 1
        and len({(candidate.booked_at.date(), _merchant_key(
            candidate.counterparty_raw or candidate.description))
                 for candidate in matches}) == 1
    ):
        # Выписка содержит одну операцию, а несколько кадров показали её
        # повторно. Все одинаковые наблюдения уступают одной строке банка.
        return matches
    if not matches and len(near_matches) == 1:
        return near_matches
    if not matches and not near_matches and len(transfer_matches) == 1:
        return transfer_matches
    return []


_GENERIC_MERCHANT = {
    "перевод", "себе в другой банк", "между своими счетами", "пополнение", "оплата",
}


def _specific_merchant_match(a: str, b: str) -> bool:
    x, y = _merchant_key(a), _merchant_key(b)
    return bool(x and y and x not in _GENERIC_MERCHANT and y not in _GENERIC_MERCHANT
                and _similar_merchant(a, b))


_PERSON_INITIAL = re.compile(r"^[а-яё]+\s+[а-яё]$", re.IGNORECASE)


def _transfer_like(value: str) -> bool:
    key = norm_key(value)
    return bool(
        "перевод" in key or "пополнение" in key
        or "быстрых платежей" in key or "себе в другой банк" in key
        or _PERSON_INITIAL.fullmatch(key)
    )


def _overlapping_screenshots(
    session: Session, user_id: int, rows: list[ParsedRow]
) -> dict[tuple[object, int, str], list[RawTransaction]]:
    """Предыдущие кадры с двумя общими покупками и тем же видимым списком."""
    if len(rows) < 2:
        return {}

    def key(day, amount, merchant):
        return (day, amount, norm_key(merchant))

    incoming = {
        key(row.booked_at.date(), row.amount, row.counterparty_raw or row.description)
        for row in rows
    }
    earlier = session.scalars(
        select(RawTransaction).where(
            RawTransaction.user_id == user_id,
            RawTransaction.source == OperationSource.SCREENSHOT,
            RawTransaction.booked_at >= min(r.booked_at for r in rows) - timedelta(days=1),
            RawTransaction.booked_at <= max(r.booked_at for r in rows) + timedelta(days=1),
        )
    ).all()
    by_batch: dict[int, set[tuple[object, int, str]]] = defaultdict(set)
    by_key: dict[tuple[object, int, str], list[RawTransaction]] = defaultdict(list)
    for candidate in earlier:
        signature = key(
            candidate.booked_at.date(), candidate.amount,
            candidate.counterparty_raw or candidate.description,
        )
        if signature in incoming:
            by_batch[candidate.batch_id].add(signature)
            by_key[signature].append(candidate)
    eligible = {batch_id for batch_id, keys in by_batch.items() if len(keys) >= 2}
    return {
        signature: [candidate for candidate in candidates if candidate.batch_id in eligible]
        for signature, candidates in by_key.items()
    }


def _find_pending_pair(
    session: Session,
    user_id: int,
    row: ParsedRow,
    account_id: int,
    *,
    current_batch_id: int,
    consumed: set[int],
) -> RawTransaction | None:
    """Связывает временную блокировку с проведённой строкой того же платежа.

    Банк часто меняет код и дату проводки, иногда окончательную сумму.
    Поэтому код операции не сравниваем; нужны один счёт, близкие день и
    сумма, совпадение продавца. Одну строку можно использовать только раз.
    """
    incoming_pending = bool(row.raw.get("pending"))
    if row.amount >= 0:
        return None
    tolerance = max(5_000, abs(row.amount) // 10)  # 50 ₽ или 10 %
    candidates = session.scalars(
        select(RawTransaction).where(
            RawTransaction.user_id == user_id,
            RawTransaction.account_id == account_id,
            RawTransaction.source == OperationSource.IMPORT,
            RawTransaction.batch_id != current_batch_id,
            RawTransaction.is_pending == (not incoming_pending),
            RawTransaction.amount < 0,
            RawTransaction.amount >= row.amount - tolerance,
            RawTransaction.amount <= row.amount + tolerance,
            RawTransaction.booked_at >= row.booked_at - PENDING_WINDOW,
            RawTransaction.booked_at <= row.booked_at + PENDING_WINDOW,
        )
    ).all()
    matches = [
        candidate for candidate in candidates
        if candidate.id not in consumed
        and _similar_merchant(
            candidate.counterparty_raw or candidate.description,
            row.counterparty_raw or row.description,
        )
    ]
    if not matches:
        return None
    return min(
        matches,
        key=lambda candidate: (
            abs((candidate.booked_at - row.booked_at).total_seconds()),
            abs(candidate.amount - row.amount),
        ),
    )


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
    channel: str = "",
    stored_as: str = "",
    content_type: str = "",
    size_bytes: int = 0,
) -> ImportResult:
    """Строки в базу одной загрузкой.

    stored_as/content_type/size_bytes/channel не участвуют в импорте и нужны
    только для следа: по записи о загрузке должно быть видно, какой именно
    файл её породил и каким путём он пришёл.
    """
    reg = AccountRegistry(session, user_id)
    batch = ImportBatch(
        user_id=user_id,
        source=source,
        filename=filename,
        rows_total=len(rows),
        period_from=min((r.booked_at.date() for r in rows), default=None),
        period_to=max((r.booked_at.date() for r in rows), default=None),
        channel=channel,
        stored_as=stored_as,
        content_type=content_type,
        size_bytes=size_bytes,
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
    # Хеши строк, которые уже лежат в базе с прошлого раза. Сами строки мы
    # не пишем, иначе сломается дедупликация, — а связь «этот файл показал
    # эти операции» без них рвётся ровно на повторной присылке.
    known_again: list[str] = []
    screenshot_overlap = (
        _overlapping_screenshots(session, user_id, rows)
        if origin == OperationSource.SCREENSHOT else {}
    )

    for row in rows:
        row.bank = canonical_bank(row.bank) or row.bank
        digest = row.dedup_hash()
        if digest in existing_hashes:
            duplicates += 1
            known_again.append(digest)
            continue

        if screenshot_overlap:
            signature = (
                row.booked_at.date(), row.amount,
                norm_key(row.counterparty_raw or row.description),
            )
            twins = [candidate for candidate in screenshot_overlap.get(signature, [])
                     if candidate.id not in consumed]
            if len(twins) == 1:
                consumed.add(twins[0].id)
                duplicates += 1
                known_again.append(twins[0].dedup_hash)
                continue

        account = resolve_account(reg, row)
        pending_pair = _find_pending_pair(
            session, user_id, row, account.id,
            current_batch_id=batch.id, consumed=consumed,
        ) if origin.is_statement else None
        if pending_pair is not None:
            consumed.add(pending_pair.id)
            if row.raw.get("pending"):
                duplicates += 1
                known_again.append(pending_pair.dedup_hash)
                continue
            _move_user_answers(session, user_id, pending_pair.dedup_hash, digest)
            existing_hashes.discard(pending_pair.dedup_hash)
            session.delete(pending_pair)
            session.flush()
            superseded += 1

        twins = _find_soft_duplicate(
            session, user_id, row, account.id, origin, consumed=consumed
        )
        if twins:
            if not origin.is_statement:
                # Приехало из быстрого источника, а выписка уже есть — она точнее.
                duplicates += 1
                # Хеш уцелевшей строки, а не присланной: искать потом надо
                # то, что действительно лежит в базе.
                known_again.append(twins[0].dedup_hash)
                continue
            # Выписка уточняет то, что раньше пришло скриншотом.
            for twin in twins:
                consumed.add(twin.id)
                _move_user_answers(session, user_id, twin.dedup_hash, digest)
                existing_hashes.discard(twin.dedup_hash)
                session.delete(twin)
            session.flush()
            superseded += len(twins)

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
            is_pending=bool(row.raw.get("pending")),
            source=origin,
        )
        session.add(raw)
        created.append(raw)

    batch.rows_new = len(created)
    batch.rows_duplicate = duplicates
    batch.duplicate_hashes = known_again
    session.flush()
    return ImportResult(
        batch=batch, new_rows=created, duplicates=duplicates, superseded=superseded
    )


def import_file(
    session: Session,
    user_id: int,
    data: bytes,
    *,
    filename: str = "",
    channel: str = "",
    stored_as: str = "",
    content_type: str = "",
) -> ImportResult:
    method = "csv"
    if data.startswith(b"%PDF-") or filename.lower().endswith(".pdf"):
        from app.ingest.pdf import parse_pdf_statement  # noqa: PLC0415

        pdf = parse_pdf_statement(data)
        bank, rows = pdf.bank, pdf.rows
        method = pdf.method
    else:
        bank, rows = parse_statement(data, filename=filename)
    if not rows:
        raise ValueError("в файле не найдено операций")
    result = import_rows(
        session, user_id, rows, source=bank, filename=filename,
        origin=OperationSource.IMPORT,
        channel=channel, stored_as=stored_as, content_type=content_type,
        size_bytes=len(data),
    )
    result.extraction_method = method
    result.pending_count = sum(row.is_pending for row in result.new_rows)
    return result


def import_images(
    session: Session,
    user_id: int,
    images: list[bytes],
    *,
    channel: str = "",
    stored_as: str = "",
    content_type: str = "",
    filename: str = "",
    **kwargs,
) -> tuple[ImportResult, str]:
    """Картинка от пользователя: скриншот банка или фото чека.

    Вид определяет модель, а не пользователь: просить выбрать в меню
    «это чек или это скриншот» — лишний шаг там, где можно обойтись без него.

    Поля следа объявлены явно, а не сложены в **kwargs: остаток kwargs
    уходит в модель зрения, и «просто дописать filename» означало бы
    передать его не в базу, а в разбор картинки.
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
            # Имени у картинки обычно нет — тогда в этом поле остаётся вид
            # картинки, как и раньше. Связь с файлом держит stored_as.
            filename=filename or result.kind, origin=origin,
            channel=channel, stored_as=stored_as, content_type=content_type,
            size_bytes=sum(len(image) for image in images),
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
    channel: str = "",
    stored_as: str = "",
    content_type: str = "",
    size_bytes: int = 0,
    filename: str = "",
) -> ImportResult:
    """Быстрый ручной или голосовой ввод — в основном наличные.

    stored_as и остальное — про файл на диске: у голосовой записки с
    телефона он есть, и без этой связи нельзя ответить «что именно я
    надиктовал».
    """
    return import_rows(
        session, user_id, rows, source="Вручную", filename=filename, origin=origin,
        channel=channel, stored_as=stored_as, content_type=content_type,
        size_bytes=size_bytes,
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
