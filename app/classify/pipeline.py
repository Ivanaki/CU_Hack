"""Классификация: сырые транзакции -> операции -> проводки.

Главное архитектурное решение: классификация — чистая функция от пары
(сырые транзакции, решения пользователя). Операции, проводки, обязательства
и вопросы полностью пересобираются при каждом запуске.

Что это даёт:
  * поменяли правило — просто пересчитали, миграции состояния не нужны;
  * ответ пользователя, пришедший позже, переписывает прошлое корректно
    (4 возврата "за ужин" задним числом превращают трату 8000 в 1600);
  * порядок поступления данных не влияет на результат.

Правила идут строго по порядку: сначала те, что склеивают несколько строк в
одно событие, потом те, что толкуют одиночные строки. Порядок важен —
перевод себе, распознанный после правила долгов, стал бы долгом.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.classify import categories as cats
from app.classify import places
from app.classify.enrich import (
    clean_merchant,
    enrich,
    extract_person_name,
    own_number_transfer,
    tbank_contract_transfer,
)
from app.classify.phone_identities import identities, transfer_phone
from app.config import Settings, get_settings
from app.ledger import build
from app.ledger.accounts import AccountRegistry, norm_key
from app.models import (
    Account,
    AccountKind,
    Claim,
    ClaimKind,
    ClaimSettlement,
    ClaimStatus,
    Counterparty,
    CounterpartyKind,
    Decision,
    Money,
    Operation,
    OperationKind,
    OperationSource,
    OperationStatus,
    Posting,
    RawTransaction,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    Subscription,
    User,
)

# Порог, ниже которого операция уходит в очередь вопросов, а не применяется молча.
_PERSON_IN_TEXT = re.compile(r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ]\.(?:\s*[А-ЯЁ]\.)?")

# Порог доверия правилу.
AUTO_THRESHOLD = 0.75
# Трата дороже этого без понятной категории — повод спросить.
LARGE_UNCATEGORIZED = 500_000  # 5000 ₽
# Любая сумма, включённая в «Не разобрано», должна иметь вопрос. Мелкие
# покупки по одному продавцу всё равно объединяются в одну карточку.


class DecisionKind:
    """Виды решений пользователя. Строки, а не enum: хранятся в JSON-полях."""

    SET_KIND = "set_kind"
    SET_CATEGORY = "set_category"
    PARTICIPANTS = "participants"
    FORCE_TRANSFER = "force_transfer"
    CASH_ALLOCATION = "cash_allocation"
    NOT_TRANSFER = "not_transfer"
    IGNORE = "ignore"
    # Человек ответил своими словами: в payload лежит его текст и категория,
    # если она из этого текста вычиталась. Записывается рядом с обычным
    # решением, а не вместо него, — классификацию ведёт обычное решение,
    # а это отвечает за то, что операция названа словами человека.
    FREE_TEXT = "free_text"


@dataclass(slots=True)
class Ask:
    """Отложенный вопрос пользователю."""

    kind: ReviewKind
    question: str
    options: list[dict]
    anchor: str
    priority: int
    operation: Operation | None = None


@dataclass
class Ctx:
    session: Session
    user: User
    reg: AccountRegistry
    settings: Settings
    rows: list[RawTransaction]
    own_ids: set[int]
    phone_rules: dict = field(default_factory=dict)
    decisions: dict[tuple[str, str], dict] = field(default_factory=dict)
    used: set[int] = field(default_factory=set)
    ops: list[Operation] = field(default_factory=list)
    asks: list[Ask] = field(default_factory=list)
    claims_by_anchor: dict[str, Claim] = field(default_factory=dict)

    @property
    def user_id(self) -> int:
        return self.user.id

    def decision(self, anchor: str, kind: str) -> dict | None:
        return self.decisions.get((anchor, kind))

    def free(self, *, incoming: bool | None = None) -> list[RawTransaction]:
        out = [r for r in self.rows if r.id not in self.used]
        if incoming is True:
            out = [r for r in out if r.amount > 0]
        elif incoming is False:
            out = [r for r in out if r.amount < 0]
        return out

    def take(self, *rows: RawTransaction) -> None:
        for row in rows:
            self.used.add(row.id)

    def new_op(
        self,
        *,
        kind: OperationKind,
        occurred_at: datetime,
        title: str,
        amount: Money,
        legs: list[RawTransaction],
        rule: str,
        confidence: float = 1.0,
        explanation: str = "",
        category: str | None = None,
        counterparty_id: int | None = None,
        source: OperationSource = OperationSource.IMPORT,
        extra: dict | None = None,
    ) -> Operation:
        op = Operation(
            user_id=self.user_id,
            kind=kind,
            status=OperationStatus.AUTO if confidence >= AUTO_THRESHOLD
            else OperationStatus.NEEDS_REVIEW,
            source=source,
            occurred_at=occurred_at,
            title=title[:255],
            amount=abs(amount),
            category_code=category,
            counterparty_id=counterparty_id,
            rule=rule,
            confidence=confidence,
            explanation=explanation,
            extra=extra or {},
        )
        if legs:
            # Якорь и счёт кладём в extra: связь legs подгружается лениво и
            # после пересоздания операций доверять ей нельзя.
            op.extra = {
                **op.extra,
                "anchor": legs[0].dedup_hash,
                "account_id": legs[0].account_id,
            }
        self.session.add(op)
        self.session.flush()
        for leg in legs:
            leg.operation_id = op.id
        self.take(*legs)
        self.ops.append(op)
        return op

    def ask(
        self,
        kind: ReviewKind,
        question: str,
        options: list[dict],
        *,
        anchor: str,
        priority: int,
        operation: Operation | None = None,
    ) -> None:
        self.asks.append(Ask(kind, question, options, anchor, priority, operation))


# --------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------


def _cp(ctx: Ctx, row: RawTransaction) -> Counterparty | None:
    if row.counterparty_id is None:
        return None
    return ctx.session.get(Counterparty, row.counterparty_id)


def _is_person(ctx: Ctx, row: RawTransaction) -> bool:
    cp = _cp(ctx, row)
    return cp is not None and cp.kind == CounterpartyKind.PERSON


def _cp_name(ctx: Ctx, row: RawTransaction) -> str:
    cp = _cp(ctx, row)
    return cp.display_name if cp else (row.counterparty_raw or row.description or "Операция")


def _account(ctx: Ctx, row: RawTransaction) -> Account:
    return ctx.session.get(Account, row.account_id)


def _category_for(row: RawTransaction) -> str | None:
    return row.mcc_category


# --------------------------------------------------------------------------
# Правило 1. Перевод между своими счетами (сценарии 1 и 2)
# --------------------------------------------------------------------------


def rule_self_transfers(ctx: Ctx) -> None:
    """Склеивает пару "ушло с одного моего счёта / пришло на другой".

    В выписках это выглядит как расход и как доход одновременно, а не
    является ни тем, ни другим. Склейка допускает расхождение сумм на
    комиссию и разницу во времени до полутора суток: банки проводят
    межбанк не мгновенно.
    """
    window = timedelta(hours=ctx.settings.self_transfer_window_hours)
    own_name = norm_key(ctx.user.display_name)

    outgoing = [r for r in ctx.free(incoming=False) if r.account_id in ctx.own_ids]
    incoming = [r for r in ctx.free(incoming=True) if r.account_id in ctx.own_ids]

    for out in outgoing:
        if out.id in ctx.used:
            continue
        if ctx.decision(out.dedup_hash, DecisionKind.NOT_TRANSFER) is not None:
            continue
        choice = ctx.decision(out.dedup_hash, DecisionKind.SET_KIND)
        if choice is not None and choice.get("kind") != "self":
            continue
        sent = -out.amount
        best: tuple[float, RawTransaction] | None = None

        for inc in incoming:
            if inc.id in ctx.used or inc.account_id == out.account_id:
                continue
            incoming_choice = ctx.decision(inc.dedup_hash, DecisionKind.SET_KIND)
            if incoming_choice is not None and incoming_choice.get("kind") != "self":
                continue
            if inc.currency != out.currency:
                continue
            gap = inc.booked_at - out.booked_at
            if not (-window <= gap <= window):
                continue
            fee = sent - inc.amount
            tolerance = max(
                ctx.settings.self_transfer_fee_abs, int(sent * ctx.settings.self_transfer_fee_ratio)
            )
            if not (0 <= fee <= tolerance):
                continue

            score = _transfer_confidence(ctx, out, inc, own_name, exact=fee == 0, gap=gap)
            if score <= 0:
                continue
            if best is None or (score, -abs(gap.total_seconds()), -fee) > (
                best[0],
                -abs((best[1].booked_at - out.booked_at).total_seconds()),
                -(sent - best[1].amount),
            ):
                best = (score, inc)

        if best is None:
            continue
        score, inc = best
        dst = _account(ctx, inc)
        src = _account(ctx, out)
        is_wallet = dst.kind == AccountKind.WALLET

        kind = OperationKind.WALLET_TOPUP if is_wallet else OperationKind.TRANSFER
        title = (
            f"Пополнение кошелька {dst.bank or dst.name}"
            if is_wallet
            else f"Перевод себе: {src.name} → {dst.name}"
        )
        why = (
            f"Списание {sent / 100:.0f} ₽ с «{src.name}» и зачисление "
            f"{inc.amount / 100:.0f} ₽ на «{dst.name}» "
            f"с разницей {abs(gap_minutes(out, inc))} мин — это один перевод между "
            f"моими счетами, а не расход и доход."
        )
        op = ctx.new_op(
            kind=kind,
            occurred_at=out.booked_at,
            title=title,
            amount=sent,
            legs=[out, inc],
            rule="self_transfer",
            confidence=score,
            explanation=why,
        )
        if is_wallet:
            build.wallet_topup(ctx.session, ctx.reg, op, src=src, wallet=dst, amount=sent)
        else:
            build.transfer(
                ctx.session, ctx.reg, op, src=src, dst=dst, sent=sent, received=inc.amount
            )

        if score < AUTO_THRESHOLD:
            ctx.ask(
                ReviewKind.IS_SELF_TRANSFER,
                f"{title} на {_rub(sent)} — это перевод между вашими счетами?",
                [
                    {"code": "yes", "label": "Да, мой перевод"},
                    {"code": "no", "label": "Нет, это разные операции"},
                ],
                anchor=out.dedup_hash,
                priority=_priority(sent, 90),
                operation=op,
            )


def gap_minutes(a: RawTransaction, b: RawTransaction) -> int:
    return int((b.booked_at - a.booked_at).total_seconds() // 60)


def _transfer_confidence(
    ctx: Ctx,
    out: RawTransaction,
    inc: RawTransaction,
    own_name: str,
    *,
    exact: bool,
    gap: timedelta,
) -> float:
    """Насколько уверенно пара является переводом себе.

    Совпадения суммы и времени мало: "отдал другу 5000" и "получил 5000 от
    другого друга" в один день выглядят так же. Нужен хотя бы один
    содержательный признак — явное указание в назначении, кошелёк
    маркетплейса или собственное имя в реквизитах.
    """
    if ctx.decision(out.dedup_hash, DecisionKind.FORCE_TRANSFER) is not None:
        return 1.0

    out_phone, inc_phone = transfer_phone(out), transfer_phone(inc)
    out_rule = ctx.phone_rules.get(out_phone) if out_phone else None
    inc_rule = ctx.phone_rules.get(inc_phone) if inc_phone else None
    # Признак «мой номер» на одной строке не может переименовать явно чужой
    # перевод в паре. Разные номера тоже нельзя отождествлять по сумме.
    if (out_rule is not None and out_rule.kind == "contact") or (
        inc_rule is not None and inc_rule.kind == "contact"
    ):
        return 0.0
    if out_phone and inc_phone and out_phone != inc_phone:
        return 0.0
    own_phone = any(rule is not None and rule.kind == "own" for rule in (out_rule, inc_rule))
    if own_phone:
        own_tokens = [token for token in own_name.split() if len(token) >= 3]
        for row in (out, inc):
            person_name = extract_person_name(f"{row.counterparty_raw} {row.description}")
            if person_name and own_tokens:
                person_tokens = [
                    token for token in norm_key(person_name).split() if len(token) >= 3
                ]
                # Совпадение одной общей фамилии не доказывает, что перевод
                # был себе. При кратком имени профиля сравниваем первое имя.
                if len(own_tokens) == 1:
                    if person_tokens and person_tokens[0] != own_tokens[0]:
                        return 0.0
                elif any(token not in own_tokens for token in person_tokens):
                    return 0.0

    dst = _account(ctx, inc)
    tags = set(out.purpose_tags) | set(inc.purpose_tags)
    evidence = 0.0

    if dst.kind == AccountKind.WALLET:
        evidence += 0.5
    if "self" in tags or "topup" in tags:
        evidence += 0.45
    if own_phone:
        evidence += 0.6
    if own_name and (
        own_name in norm_key(out.counterparty_raw) or own_name in norm_key(inc.counterparty_raw)
    ):
        evidence += 0.45
    if "transfer" in tags:
        evidence += 0.1
    # Перевод человеку — сильный довод против: это долг или оплата, не себе.
    if (
        _is_person(ctx, out) and not own_phone
        and not (own_name and own_name in norm_key(out.counterparty_raw))
    ):
        evidence -= 0.25
    if {"debt", "return", "share_meal", "split", "gift"} & tags:
        evidence -= 0.4

    if evidence <= 0:
        return 0.0
    base = 0.5 if exact else 0.4
    if abs(gap) <= timedelta(hours=2):
        base += 0.1
    return min(0.99, base + evidence)


# --------------------------------------------------------------------------
# Правило 2. Снятие наличных (сценарий 7)
# --------------------------------------------------------------------------


def rule_wallet_topup_alone(ctx: Ctx) -> None:
    """Пополнение кошелька маркетплейса, у которого нет пары в выписках.

    Если выписка самого маркетплейса ещё не загружена, второй половины
    перевода не существует, и правило склейки его не находит. Раньше такое
    пополнение падало в обычные траты и получало выдуманную категорию —
    ровно то, в чём кейс упрекает автоматическую классификацию.

    Правильный ответ: пополнение своего кошелька никогда не трата. Деньги
    переезжают на счёт кошелька и ждут там. Появится выписка маркетплейса —
    покупки спишутся с этого счёта своими категориями; не появится — остаток
    честно висит на кошельке и виден как непотраченное, а не как еда.
    """
    from app.classify.categories import WALLET_MERCHANTS  # noqa: PLC0415

    wallet_keys = [norm_key(m) for m in WALLET_MERCHANTS]

    for row in ctx.free(incoming=False):
        text = norm_key(f"{row.counterparty_raw} {row.description}")
        if not any(w and w in text for w in wallet_keys):
            continue
        # «Пополнение» или «перевод» — иначе это обычная покупка на
        # маркетплейсе, а её надо оставить тратой.
        tags = set(row.purpose_tags)
        if not ({"topup", "self", "transfer"} & tags):
            continue

        amount = -row.amount
        merchant = next(
            (m for m, key in zip(WALLET_MERCHANTS, wallet_keys, strict=False) if key in text),
            "Маркетплейс",
        )
        wallet = ctx.reg.wallet_account(merchant.title())
        op = ctx.new_op(
            kind=OperationKind.WALLET_TOPUP,
            occurred_at=row.booked_at,
            title=f"Пополнение кошелька {merchant.title()}",
            amount=amount,
            legs=[row],
            rule="wallet_topup_alone",
            confidence=0.85,
            explanation=(
                "Пополнение своего кошелька на маркетплейсе — перемещение денег, "
                "а не трата. Выписки маркетплейса пока нет, поэтому сумма ждёт "
                "на счёте кошелька: когда появятся покупки, они спишутся отсюда "
                "уже со своими категориями."
            ),
        )
        build.wallet_topup(
            ctx.session, ctx.reg, op, src=_account(ctx, row), wallet=wallet, amount=amount
        )


# Как банк сам называет перемещение между счетами одного человека. Это не
# догадка по имени получателя, а прямое утверждение выписки: строка уже
# говорит, что деньги остались у хозяина. На боевой выгрузке Т-Банка таких
# строк на 360 000 ₽ за месяц, и без этого правила они все уезжали в траты —
# ровно та ошибка, ради которой существует продукт.
_OWN_ACCOUNTS_RE = re.compile(
    r"\bперевод\s+себе\b|между\s+своими\s+счетами|перевод\s+между\s+счетами|"
    r"на\s+(?:свой|собственный)\s+счёт|со\s+своего\s+счёта|"
    r"с\s+карты\s+на\s+(?:свой\s+)?(?:счёт|вклад|накопительный)",
    re.IGNORECASE,
)


def rule_own_accounts_by_text(ctx: Ctx) -> None:
    """Строка, которая сама себя называет переводом между своими счетами.

    Правило склейки требует обе половины пары; в выгрузке одного счёта
    второй половины нет по построению — человек выгрузил одну карту.
    Но банк уже написал словами, что деньги не ушли наружу, и спорить с
    этим нечего: тратой это не станет ни при каком ответе.

    Деньги переезжают на «Другие мои счета» — сумма остаётся видимой как
    непотраченное, а не растворяется в расходах.
    """
    for row in ctx.free():
        text = f"{row.counterparty_raw} {row.description}"
        choice = ctx.decision(row.dedup_hash, DecisionKind.SET_KIND)
        user_said_self = choice is not None and choice.get("kind") == "self"
        bank_said_self = bool(
            _OWN_ACCOUNTS_RE.search(text)
            or own_number_transfer(text)
            or tbank_contract_transfer(text, _account(ctx, row).bank or "")
        )
        phone = transfer_phone(row)
        identity = ctx.phone_rules.get(phone) if phone else None
        own_phone = identity is not None and identity.kind == "own"
        if not (bank_said_self or user_said_self or own_phone):
            continue
        if choice is not None and choice.get("kind") != "self":
            continue
        other = ctx.reg.get_or_create(
            kind=AccountKind.DEPOSIT, key="own:other", name="Другие мои счета"
        )
        amount = abs(row.amount)
        outgoing = row.amount < 0
        if user_said_self:
            evidence_text = "Вы указали, что это перевод между вашими счетами. "
        elif own_phone and not bank_said_self:
            evidence_text = "Номер в реквизитах вы сохранили как свой. "
        else:
            evidence_text = "Банк пометил операцию как перевод между вашими счетами. "
        op = ctx.new_op(
            kind=OperationKind.TRANSFER,
            occurred_at=row.booked_at,
            title="Перевод между своими счетами",
            amount=amount,
            legs=[row],
            rule="user_decision" if user_said_self else "own_accounts_by_text",
            confidence=0.95,
            explanation=(
                evidence_text
                + "Деньги остались у вас, поэтому сумма не входит в расходы и доходы."
            ),
        )
        if user_said_self:
            op.status = OperationStatus.CONFIRMED
        mine = _account(ctx, row)
        src, dst = (mine, other) if outgoing else (other, mine)
        build.transfer(ctx.session, ctx.reg, op, src=src, dst=dst, sent=amount, received=amount)


def rule_atm(ctx: Ctx) -> None:
    for row in ctx.free(incoming=False):
        mcc = (row.mcc or "").zfill(4) if row.mcc else ""
        if mcc not in cats.MCC_ATM and "atm" not in row.purpose_tags:
            continue
        amount = -row.amount
        op = ctx.new_op(
            kind=OperationKind.CASH_WITHDRAWAL,
            occurred_at=row.booked_at,
            title="Снятие наличных",
            amount=amount,
            legs=[row],
            rule="atm",
            explanation=(
                "Снятие в банкомате — перемещение денег между моими активами, "
                "а не трата. Тратой они станут, когда будет известно, на что ушли."
            ),
        )
        build.cash_withdrawal(ctx.session, ctx.reg, op, account=_account(ctx, row), amount=amount)


# --------------------------------------------------------------------------
# Правило 3. Возврат товара (сценарий 6)
# --------------------------------------------------------------------------


def rule_refunds(ctx: Ctx) -> None:
    """Входящее от магазина, совпадающее с прошлой покупкой у него же.

    Это не доход: это отмена уже учтённой траты. Проводка идёт датой
    возврата, а не датой покупки — возврат является отдельным решением,
    и закрытые периоды из-за него не переписываются.
    """
    window = timedelta(days=ctx.settings.refund_match_window_days)
    spent = [
        op
        for op in ctx.ops
        if op.kind == OperationKind.EXPENSE and op.counterparty_id is not None
    ]
    matched: set[int] = set()

    for row in ctx.free(incoming=True):
        if _is_person(ctx, row):
            continue
        if "return" not in row.purpose_tags and row.counterparty_id is None:
            continue
        candidates = [
            op
            for op in spent
            if op.id not in matched
            and op.counterparty_id == row.counterparty_id
            and timedelta(0) <= (row.booked_at - op.occurred_at) <= window
            and op.amount >= row.amount
        ]
        if not candidates:
            continue
        # Ближайшая по сумме, затем самая свежая: возвращают обычно последнее.
        original = min(candidates, key=lambda o: (abs(o.amount - row.amount), -o.id))
        matched.add(original.id)

        op = ctx.new_op(
            kind=OperationKind.REFUND,
            occurred_at=row.booked_at,
            title=f"Возврат: {_cp_name(ctx, row)}",
            amount=row.amount,
            legs=[row],
            rule="refund",
            category=original.category_code,
            counterparty_id=row.counterparty_id,
            explanation=(
                f"Совпало с покупкой от {original.occurred_at:%d.%m} на "
                f"{original.amount / 100:.0f} ₽ у того же продавца. Это не доход, "
                f"а уменьшение траты в категории «{cats.title(original.category_code)}»."
            ),
            extra={"original_operation_id": original.id},
        )
        build.refund(
            ctx.session,
            ctx.reg,
            op,
            account=_account(ctx, row),
            amount=row.amount,
            category=original.category_code,
        )


# --------------------------------------------------------------------------
# Правило 4. Долги: выдача и возврат (сценарий 3)
# --------------------------------------------------------------------------


def rule_debt_out(ctx: Ctx) -> None:
    """Исходящий перевод человеку.

    Явный признак долга в назначении — выдаём долг. Без признаков смысл
    неизвестен, и мы не гадаем: операция висит на suspense и попадает в
    очередь вопросов. Это честнее, чем записать 3000 в "Прочее" и тихо
    испортить статистику.
    """
    for row in ctx.free(incoming=False):
        if not _is_person(ctx, row):
            continue
        amount = -row.amount
        tags = set(row.purpose_tags)
        name = _cp_name(ctx, row)
        forced = ctx.decision(row.dedup_hash, DecisionKind.SET_KIND)

        if forced:
            _apply_forced_outgoing(ctx, row, forced, amount, name)
            continue

        # Порядок проверок значим: «возвращаю долг» содержит слово «долг»,
        # и без этой очерёдности погашение превратилось бы в новую выдачу.
        borrowed = _open_claim(ctx, row.counterparty_id, ClaimKind.DEBT_BORROWED)
        if "return" in tags and borrowed is not None:
            claim = borrowed
            op = ctx.new_op(
                kind=OperationKind.DEBT_REPAY_OUT,
                occurred_at=row.booked_at,
                title=f"Вернул долг: {name}",
                amount=amount,
                legs=[row],
                rule="debt_repay_out",
                counterparty_id=row.counterparty_id,
                explanation="Гашение моего обязательства, а не трата.",
            )
            build.debt_repay_out(
                ctx.session, ctx.reg, op,
                account=_account(ctx, row), payable=claim.account, amount=amount,
            )
            _settle(ctx, claim, op, amount)
            continue
        if "debt" in tags:
            _make_debt_lend(ctx, row, amount, name, confidence=0.93)
            continue
        if row.mcc_category and not (tags & {"debt", "return"}):
            # Назначение само объясняет трату: "Аренда квартиры Смирнова Е. В."
            # — это жильё, а не загадочный перевод человеку. Отдаём в
            # обычные траты, чтобы не спрашивать про очевидное каждый месяц.
            continue
        if "gift" in tags:
            op = ctx.new_op(
                kind=OperationKind.EXPENSE,
                occurred_at=row.booked_at,
                title=f"Подарок: {name}",
                amount=amount,
                legs=[row],
                rule="gift",
                category="gifts",
                counterparty_id=row.counterparty_id,
                explanation="В назначении указан подарок.",
            )
            build.expense(
                ctx.session, ctx.reg, op,
                account=_account(ctx, row), amount=amount, category="gifts",
            )
            continue

        # Смысл неизвестен — спрашиваем, но деньги не приписываем ни к чему.
        op = ctx.new_op(
            kind=OperationKind.UNKNOWN,
            occurred_at=row.booked_at,
            title=f"Перевод: {name}",
            amount=amount,
            legs=[row],
            rule="outgoing_to_person",
            confidence=0.3,
            counterparty_id=row.counterparty_id,
            explanation=(
                "Перевод физлицу без указания назначения. Пока смысл неизвестен, "
                "сумма не попадает ни в расходы, ни в переводы."
            ),
        )
        build.unknown(ctx.session, ctx.reg, op, account=_account(ctx, row), amount=row.amount)
        ctx.ask(
            ReviewKind.OUTGOING_TO_PERSON,
            f"Перевод {name} на {_rub(amount)} — что это?",
            [
                {"code": "debt", "label": "Дал в долг"},
                {"code": "gift", "label": "Подарок"},
                {"code": "service", "label": "Оплата услуг"},
                {"code": "self", "label": "Себе на другой счёт"},
            ],
            anchor=row.dedup_hash,
            priority=_priority(amount, 100),
            operation=op,
        )


def _apply_forced_outgoing(
    ctx: Ctx, row: RawTransaction, forced: dict, amount: Money, name: str
) -> None:
    """Пользователь уже ответил про эту операцию — применяем его ответ."""
    choice = forced.get("kind")
    if choice == "debt":
        _make_debt_lend(ctx, row, amount, name, confidence=1.0, confirmed=True)
        return
    if choice == "self":
        # Человек сказал, что это перевод себе. Второй половины пары в
        # выписках нет (иначе правило склейки нашло бы её само), поэтому
        # деньги переезжают на счёт «другие мои счета» — и не становятся
        # тратой. Записать их расходом означало бы ровно то, за что кейс
        # снимает баллы.
        other = ctx.reg.get_or_create(
            kind=AccountKind.DEPOSIT,
            key="own:other",
            name="Другие мои счета",
        )
        op = ctx.new_op(
            kind=OperationKind.TRANSFER,
            occurred_at=row.booked_at,
            title=f"Перевод себе: {name}",
            amount=amount,
            legs=[row],
            rule="user_decision",
            counterparty_id=row.counterparty_id,
            explanation=(
                "Вы указали, что это перевод на свой счёт. Пары в загруженных "
                "выписках нет, поэтому деньги учтены как перемещение на другой "
                "ваш счёт. В расходы они не попадают."
            ),
        )
        op.status = OperationStatus.CONFIRMED
        build.transfer(
            ctx.session, ctx.reg, op,
            src=_account(ctx, row), dst=other, sent=amount, received=amount,
        )
        return
    category = {"gift": "gifts", "service": cats.OTHER}.get(choice, cats.OTHER)
    category = forced.get("category") or category
    op = ctx.new_op(
        kind=OperationKind.EXPENSE,
        occurred_at=row.booked_at,
        title=f"{'Подарок' if choice == 'gift' else 'Оплата'}: {name}",
        amount=amount,
        legs=[row],
        rule="user_decision",
        category=category,
        counterparty_id=row.counterparty_id,
        explanation="Классифицировано вашим ответом.",
    )
    op.status = OperationStatus.CONFIRMED
    op.explained = True
    build.expense(
        ctx.session, ctx.reg, op, account=_account(ctx, row), amount=amount, category=category
    )


def _make_debt_lend(
    ctx: Ctx,
    row: RawTransaction,
    amount: Money,
    name: str,
    *,
    confidence: float,
    confirmed: bool = False,
) -> None:
    op = ctx.new_op(
        kind=OperationKind.DEBT_LEND,
        occurred_at=row.booked_at,
        title=f"Дал в долг: {name}",
        amount=amount,
        legs=[row],
        rule="debt_lend",
        confidence=confidence,
        counterparty_id=row.counterparty_id,
        explanation=(
            "Выдача долга — не расход: деньги сменили форму, а не исчезли. "
            "Возвраты, в том числе частичные, будут сопоставлены с этой выдачей."
        ),
    )
    if confirmed:
        op.status = OperationStatus.CONFIRMED
    slug = f"debt:{row.counterparty_id}:{row.dedup_hash[:10]}"
    receivable = ctx.reg.receivable(slug, f"Долг: {name}", row.counterparty_id)
    claim = Claim(
        user_id=ctx.user_id,
        kind=ClaimKind.DEBT_LENT,
        counterparty_id=row.counterparty_id,
        account_id=receivable.id,
        principal=amount,
        opened_at=row.booked_at,
        deadline_at=None,
        source_operation_id=op.id,
        label=f"Долг: {name}",
    )
    ctx.session.add(claim)
    ctx.session.flush()
    op.claim_id = claim.id
    build.debt_lend(
        ctx.session, ctx.reg, op,
        account=_account(ctx, row), receivable=receivable, amount=amount,
    )


def rule_borrow_in(ctx: Ctx) -> None:
    """Мне дали в долг: деньги пришли, но это не доход, а обязательство.

    Срабатывает на входящем от человека с явной пометкой про долг, когда
    своей открытой выдачи этому человеку нет — значит, занимаю я. Без
    этого правила заём попадал бы в доходы и завышал бы картину.
    """
    for row in sorted(ctx.free(incoming=True), key=lambda r: r.booked_at):
        if not _is_person(ctx, row):
            continue
        tags = set(row.purpose_tags)
        if "debt" not in tags or "return" in tags:
            continue
        if _open_claim(ctx, row.counterparty_id, ClaimKind.DEBT_LENT):
            continue  # это возврат моего долга, им займётся следующее правило

        name = _cp_name(ctx, row)
        op = ctx.new_op(
            kind=OperationKind.DEBT_BORROW,
            occurred_at=row.booked_at,
            title=f"Занял у: {name}",
            amount=row.amount,
            legs=[row],
            rule="debt_borrow",
            confidence=0.85,
            counterparty_id=row.counterparty_id,
            explanation=(
                "Заём — не доход: эти деньги придётся вернуть. "
                "Мой последующий перевод этому человеку будет зачтён как погашение."
            ),
        )
        slug = f"payable:{row.counterparty_id}:{row.dedup_hash[:10]}"
        payable = ctx.reg.payable(slug, f"Долг перед: {name}", row.counterparty_id)
        claim = Claim(
            user_id=ctx.user_id,
            kind=ClaimKind.DEBT_BORROWED,
            counterparty_id=row.counterparty_id,
            account_id=payable.id,
            principal=row.amount,
            opened_at=row.booked_at,
            source_operation_id=op.id,
            label=f"Долг перед: {name}",
        )
        ctx.session.add(claim)
        ctx.session.flush()
        op.claim_id = claim.id
        build.debt_borrow(
            ctx.session, ctx.reg, op,
            account=_account(ctx, row), payable=payable, amount=row.amount,
        )


def rule_debt_in(ctx: Ctx) -> None:
    """Входящее от человека, сопоставляемое с открытым долгом.

    Сопоставление жадное, от самого раннего долга: поддерживает частичный
    возврат и возврат с задержкой (сценарий 3 — 3000 возвращаются двумя
    частями по 1500 с интервалом в неделю).
    """
    window = timedelta(days=ctx.settings.debt_match_window_days)
    for row in sorted(ctx.free(incoming=True), key=lambda r: r.booked_at):
        if not _is_person(ctx, row):
            continue
        claims = _open_claims(ctx, row.counterparty_id, ClaimKind.DEBT_LENT)
        claims = [c for c in claims if timedelta(0) <= (row.booked_at - c.opened_at) <= window]
        if not claims:
            continue
        tags = set(row.purpose_tags)
        # Возврат без пояснения тоже принимаем, если долг открыт: это самая
        # вероятная трактовка, и она не создаёт фантомного дохода.
        if {"share_meal", "share_ride", "split", "gift", "salary"} & tags:
            continue

        remaining = row.amount
        name = _cp_name(ctx, row)
        legs_used = False
        for claim in claims:
            if remaining <= 0:
                break
            portion = min(remaining, claim.outstanding)
            if portion <= 0:
                continue
            op = ctx.new_op(
                kind=OperationKind.DEBT_REPAY_IN,
                occurred_at=row.booked_at,
                title=f"Возврат долга: {name}",
                amount=portion,
                legs=[row] if not legs_used else [],
                rule="debt_repay_in",
                confidence=0.9,
                counterparty_id=row.counterparty_id,
                explanation=(
                    f"Сопоставлено с выдачей от {claim.opened_at:%d.%m} на "
                    f"{claim.principal / 100:.0f} ₽. "
                    f"Это не доход: возвращаются мои же деньги."
                ),
            )
            legs_used = True
            op.claim_id = claim.id
            build.debt_repay_in(
                ctx.session, ctx.reg, op,
                account=_account(ctx, row), receivable=claim.account, amount=portion,
            )
            _settle(ctx, claim, op, portion)
            remaining -= portion

        if remaining > 0:
            # Объяснить нужно только излишек: закрытая часть долга уже
            # проведена и не должна второй раз стать доходом или расходом.
            forced = ctx.decision(row.dedup_hash, DecisionKind.SET_KIND)
            if forced:
                _apply_forced_incoming(
                    ctx, row, forced, name, amount=remaining, residual=True
                )
            else:
                op = ctx.new_op(
                    kind=OperationKind.UNKNOWN,
                    occurred_at=row.booked_at,
                    title=f"Поступление сверх долга: {name}",
                    amount=remaining,
                    legs=[],
                    rule="debt_overpay",
                    confidence=0.3,
                    counterparty_id=row.counterparty_id,
                    explanation="Сумма превышает открытый долг, остаток не разобран.",
                    extra={"anchor": row.dedup_hash},
                )
                build.unknown(
                    ctx.session, ctx.reg, op, account=_account(ctx, row), amount=remaining
                )
                ctx.ask(
                    ReviewKind.INCOMING_UNKNOWN,
                    f"Сверх возвращённого долга пришло {_rub(remaining)} от {name}. Что это?",
                    _incoming_options(),
                    anchor=row.dedup_hash,
                    priority=_priority(remaining, 95),
                    operation=op,
                )


# --------------------------------------------------------------------------
# Правило 5. Общий счёт: оплата за компанию (сценарий 4)
# --------------------------------------------------------------------------


def _cluster_reimbursements(rows: list[RawTransaction]) -> list[list[RawTransaction]]:
    """Собирает поступления, похожие на доли одного счёта.

    Люди, скидывающиеся на общий счёт, переводят примерно одинаковые суммы
    в течение нескольких дней. Группировка именно по этому признаку, а не
    по близости к какой-нибудь трате: иначе перевод «за ужин» цепляется за
    случайный поход в магазин, который оказался чуть ближе по времени.
    """
    clusters: list[list[RawTransaction]] = []
    for row in sorted(rows, key=lambda r: r.booked_at):
        placed = False
        for cluster in clusters:
            head = cluster[0]
            same_amount = abs(row.amount - head.amount) <= max(5_000, head.amount // 20)
            close_in_time = abs((row.booked_at - head.booked_at).days) <= 10
            new_payer = row.counterparty_id not in {r.counterparty_id for r in cluster}
            if same_amount and close_in_time and new_payer:
                cluster.append(row)
                placed = True
                break
        if not placed:
            clusters.append([row])
    return clusters


def _match_bill(
    cluster: list[RawTransaction], candidates: list[Operation], window: timedelta
) -> Operation | None:
    """Ищет трату, долями которой является группа поступлений.

    Счёт на компанию делится примерно поровну, поэтому у настоящего
    кандидата сумма близка к доле, умноженной на число участников. Это
    гораздо надёжнее, чем просто «ближайшая предыдущая трата».
    """
    share = sum(r.amount for r in cluster) // len(cluster)
    earliest = min(r.booked_at for r in cluster)
    people = len(cluster)

    best: tuple[float, Operation] | None = None
    for op in candidates:
        gap = earliest - op.occurred_at
        if not (timedelta(0) <= gap <= window):
            continue
        if op.amount <= sum(r.amount for r in cluster):
            continue  # счёт должен быть больше суммы возвращённых долей
        # Сколько человек объясняют этот счёт при такой доле.
        heads = op.amount / share if share else 0
        if not (people + 0.5 <= heads <= people + 4.5):
            continue
        # Чем ближе к целому числу участников, тем правдоподобнее.
        integrality = abs(heads - round(heads))
        recency = gap.total_seconds() / window.total_seconds()
        score = integrality * 2 + recency
        if best is None or score < best[0]:
            best = (score, op)
    return best[1] if best else None


def rule_shared_bill(ctx: Ctx) -> None:
    """Задним числом превращает крупную трату в оплату за компанию.

    Триггер — не менее двух поступлений от разных людей с назначением про
    еду, поездку или сбор, привязанных к одной крупной трате. Одного
    поступления мало: это сценарий 5 (500 "за обед" без зафиксированного
    долга), и на него надо спросить, а не догадываться.
    """
    window = timedelta(days=14)
    candidates = [
        op
        for op in ctx.ops
        if op.kind == OperationKind.EXPENSE
        and op.amount >= 100_000
        and op.category_code in {"cafe", "delivery", "taxi", "travel", "entertainment", "groceries"}
    ]
    if not candidates:
        return

    incoming = [
        r
        for r in ctx.free(incoming=True)
        if _is_person(ctx, r)
        and {"share_meal", "share_ride", "split", "bill", "share_rent"} & set(r.purpose_tags)
    ]
    if not incoming:
        return

    groups: dict[int, list[RawTransaction]] = {}
    for cluster in _cluster_reimbursements(incoming):
        bill = _match_bill(cluster, candidates, window)
        if bill is not None:
            groups.setdefault(bill.id, []).extend(cluster)

    for op_id, rows in groups.items():
        bill = ctx.session.get(Operation, op_id)
        payers = {r.counterparty_id for r in rows}
        anchor = bill.extra.get("anchor") or f"op:{bill.id}"
        forced = ctx.decision(anchor, DecisionKind.PARTICIPANTS)

        if len(payers) < 2 and not forced:
            continue  # один плательщик — спросим отдельно, см. rule_unknown_incoming

        # Число могло и не прозвучать: на вопрос «на скольких делили» человек
        # вправе ответить своими словами. Тогда остаётся оценка по плательщикам.
        participants = int((forced or {}).get("n") or 0) or len(payers) + 1
        participants = max(2, participants)
        total = bill.amount
        my_share = total // participants
        others = total - my_share

        slug = f"shared:{anchor[:12]}"
        receivable = ctx.reg.receivable(slug, f"Общий счёт: {bill.title}")
        claim = Claim(
            user_id=ctx.user_id,
            kind=ClaimKind.SHARED_BILL,
            account_id=receivable.id,
            principal=others,
            total_bill=total,
            participants=participants,
            my_share=my_share,
            opened_at=bill.occurred_at,
            deadline_at=bill.occurred_at
            + timedelta(days=ctx.settings.shared_bill_deadline_days),
            source_operation_id=bill.id,
            label=f"Общий счёт: {bill.title}",
        )
        ctx.session.add(claim)
        ctx.session.flush()

        bill.kind = OperationKind.SHARED_BILL
        bill.claim_id = claim.id
        bill.rule = "shared_bill"
        bill.status = OperationStatus.CONFIRMED if forced else OperationStatus.AUTO
        bill.explanation = (
            f"Счёт {total / 100:.0f} ₽ разделён на {participants} чел.: моя доля "
            f"{my_share / 100:.0f} ₽, остальные {others / 100:.0f} ₽ — "
            f"долг компании. В расходы попадает только моя доля."
        )
        build.shared_bill(
            ctx.session, ctx.reg, bill,
            account=ctx.session.get(Account, bill.extra["account_id"]),
            receivable=receivable,
            total=total,
            my_share=my_share,
            category=bill.category_code,
        )

        for row in sorted(rows, key=lambda r: r.booked_at):
            portion = min(row.amount, claim.outstanding)
            if portion <= 0:
                continue
            op = ctx.new_op(
                kind=OperationKind.REIMBURSEMENT_IN,
                occurred_at=row.booked_at,
                title=f"Доля: {_cp_name(ctx, row)}",
                amount=portion,
                legs=[row],
                rule="reimbursement",
                confidence=0.9,
                counterparty_id=row.counterparty_id,
                explanation=(
                    f"Компенсация доли по счёту от {bill.occurred_at:%d.%m}. "
                    f"Не доход: гасит долг компании передо мной."
                ),
            )
            op.claim_id = claim.id
            build.reimbursement_in(
                ctx.session, ctx.reg, op,
                account=_account(ctx, row), receivable=receivable, amount=portion,
            )
            _settle(ctx, claim, op, portion)

        if not forced:
            ctx.ask(
                ReviewKind.SHARED_BILL_HEADS,
                (
                    f"«{bill.title}» на {_rub(total)} — на скольких человек делили? "
                    f"Пока считаю, что на {participants}."
                ),
                [{"code": str(n), "label": f"{n} чел."} for n in (2, 3, 4, 5)]
                + [{"code": "more", "label": "Больше"}],
                anchor=anchor,
                priority=_priority(total, 80),
                operation=bill,
            )


# --------------------------------------------------------------------------
# Правило 6. Непонятное поступление (сценарий 5)
# --------------------------------------------------------------------------


def _incoming_options() -> list[dict]:
    return [
        {"code": "share", "label": "Вернули долю"},
        {"code": "debt_back", "label": "Вернули долг"},
        {"code": "gift", "label": "Подарок"},
        {"code": "income", "label": "Это доход"},
    ]


def rule_unknown_incoming(ctx: Ctx) -> None:
    """Пришли деньги от человека, а зафиксированного долга нет.

    Ровно сценарий 5 кейса. Записать это доходом — соврать. Поэтому вторая
    нога висит на suspense, сумма видна как неразобранная, а пользователю
    уходит один вопрос с четырьмя кнопками.
    """
    for row in ctx.free(incoming=True):
        if not _is_person(ctx, row):
            continue
        name = _cp_name(ctx, row)
        forced = ctx.decision(row.dedup_hash, DecisionKind.SET_KIND)
        if forced:
            _apply_forced_incoming(ctx, row, forced, name)
            continue

        hint = ""
        if {"share_meal", "share_ride", "split"} & set(row.purpose_tags):
            hint = (
                " В назначении есть пометка про совместную трату, "
                "но подходящего счёта не нашлось."
            )

        op = ctx.new_op(
            kind=OperationKind.UNKNOWN,
            occurred_at=row.booked_at,
            title=f"Поступление: {name}",
            amount=row.amount,
            legs=[row],
            rule="unknown_incoming",
            confidence=0.25,
            counterparty_id=row.counterparty_id,
            explanation=(
                f"Поступление от физлица без зафиксированного долга.{hint} "
                f"Пока не отнесено ни к доходу, ни к возврату — иначе цифры бы соврали."
            ),
        )
        build.unknown(ctx.session, ctx.reg, op, account=_account(ctx, row), amount=row.amount)
        ctx.ask(
            ReviewKind.INCOMING_UNKNOWN,
            # Формулировка без рода: пол контрагента из выписки неизвестен,
            # а «перевёл» половине пользователей читается как ошибка.
            (
                f"Пришло {_rub(row.amount)} от {name}"
                + (f", назначение «{_purpose_note(row)}»" if _purpose_note(row) else "")
                + " — что это?"
            ),
            _incoming_options(),
            anchor=row.dedup_hash,
            priority=_priority(row.amount, 95),
            operation=op,
        )


def _apply_forced_incoming(
    ctx: Ctx, row: RawTransaction, forced: dict, name: str,
    *, amount: Money | None = None, residual: bool = False,
) -> None:
    choice = forced.get("kind")
    account = _account(ctx, row)
    value = row.amount if amount is None else amount
    legs = [] if residual else [row]
    extra = {"anchor": row.dedup_hash} if residual else None

    if choice in {"share", "debt_back"}:
        # Возврат моих же денег: гасим или, если гасить нечего, просто не
        # считаем доходом — заводим техническую дебиторку задним числом.
        slug = f"debt:{row.counterparty_id}:manual"
        receivable = ctx.reg.receivable(slug, f"Долг: {name}", row.counterparty_id)
        op = ctx.new_op(
            kind=OperationKind.DEBT_REPAY_IN,
            occurred_at=row.booked_at,
            title=f"Возврат: {name}",
            amount=value,
            legs=legs,
            rule="user_decision",
            counterparty_id=row.counterparty_id,
            explanation="Вы указали, что это возврат ваших денег.",
            extra=extra,
        )
        op.status = OperationStatus.CONFIRMED
        build.debt_repay_in(
            ctx.session, ctx.reg, op,
            account=account, receivable=receivable, amount=value,
        )
        return

    category = "gift_income" if choice == "gift" else cats.OTHER_INCOME
    category = forced.get("category") or category
    op = ctx.new_op(
        kind=OperationKind.INCOME,
        occurred_at=row.booked_at,
        title=f"Поступление: {name}",
        amount=value,
        legs=legs,
        rule="user_decision",
        category=category,
        counterparty_id=row.counterparty_id,
        explanation="Классифицировано вашим ответом.",
        extra=extra,
    )
    op.status = OperationStatus.CONFIRMED
    build.income(ctx.session, ctx.reg, op, account=account, amount=value, category=category)


# --------------------------------------------------------------------------
# Правило 7. Всё остальное
# --------------------------------------------------------------------------


def rule_fallback_expenses(ctx: Ctx) -> None:
    """Всё оставшееся исходящее — обычные траты."""
    for row in ctx.free(incoming=False):
        account = _account(ctx, row)
        name = _cp_name(ctx, row)
        override = ctx.decision(row.dedup_hash, DecisionKind.SET_CATEGORY)

        amount = -row.amount
        category = (override or {}).get("category") or _category_for(row)
        is_cash = account.kind == AccountKind.CASH
        op = ctx.new_op(
            kind=OperationKind.CASH_EXPENSE if is_cash else OperationKind.EXPENSE,
            occurred_at=row.booked_at,
            title=name,
            amount=amount,
            legs=[row],
            rule="category_rules" if category else "uncategorized",
            confidence=1.0 if category else 0.6,
            category=category or cats.OTHER,
            counterparty_id=row.counterparty_id,
            source=OperationSource(row.source),
            explanation=(
                f"Категория «{cats.title(category)}» выбрана вами для этой траты."
                if override else _why_category(row, category)
            ),
        )
        if override:
            op.status = OperationStatus.CONFIRMED
            op.explained = True
        if is_cash:
            build.cash_expense(
                ctx.session, ctx.reg, op, amount=amount, category=category or cats.OTHER
            )
        else:
            build.expense(
                ctx.session, ctx.reg, op,
                account=account, amount=amount, category=category or cats.OTHER,
            )
        if not category and amount >= LARGE_UNCATEGORIZED and not override:
            ctx.ask(
                ReviewKind.LARGE_UNCATEGORIZED,
                f"{name} — {_rub(amount)}. К какой категории отнести?",
                _category_options(),
                anchor=row.dedup_hash,
                priority=_priority(amount, 70),
                operation=op,
            )


def rule_fallback_income(ctx: Ctx) -> None:
    """Всё оставшееся входящее — доходы. Выполняется последним: к этому
    моменту возвраты, долги и доли по общим счетам уже разобраны."""
    for row in ctx.free(incoming=True):
        account = _account(ctx, row)
        name = _cp_name(ctx, row)
        category = _income_category(row)
        op = ctx.new_op(
            kind=OperationKind.INCOME,
            occurred_at=row.booked_at,
            title=name,
            amount=row.amount,
            legs=[row],
            rule="income",
            confidence=0.9,
            category=category,
            counterparty_id=row.counterparty_id,
            explanation="Поступление от организации, отнесено к доходам.",
        )
        build.income(
            ctx.session, ctx.reg, op,
            account=account, amount=row.amount, category=category,
        )


def _income_category(row: RawTransaction) -> str:
    tags = set(row.purpose_tags)
    for tag, code in (
        ("salary", "salary"),
        ("cashback", "cashback"),
        ("interest", "interest"),
        ("gift", "gift_income"),
    ):
        if tag in tags:
            return code
    return cats.OTHER_INCOME


def _why_category(row: RawTransaction, category: str | None) -> str:
    if not category:
        return "Категория не определена: нет ни MCC, ни узнаваемого названия продавца."
    if row.mcc:
        return f"Категория «{cats.title(category)}» по коду MCC {row.mcc}."
    return f"Категория «{cats.title(category)}» по названию продавца."


def _category_options() -> list[dict]:
    """Кнопки выбора категории. Без эмодзи: в узкой кнопке они только шумят."""
    codes = ("groceries", "cafe", "taxi", "clothes", "electronics", "health", "home", "other")
    return [{"code": c, "label": cats.title(c)} for c in codes]


def _place_options() -> list[dict]:
    """Кнопки для вопроса «что это за место».

    Без «Прочего»: ответ «прочее» ничего не объясняет, трата осталась бы
    неясной, и тот же вопрос вернулся бы следующим же пересчётом. Человеку,
    которому ни одна кнопка не подходит, отвечает «Своими словами» —
    она есть у каждого вопроса.
    """
    codes = (
        "groceries", "cafe", "delivery", "taxi", "health",
        "beauty", "entertainment", "sport", "clothes", "home",
    )
    return [{"code": c, "label": cats.title(c)} for c in codes]


def _times(count: int) -> str:
    """«за 1 операцию» / «за 4 операции» / «за 7 операций»."""
    tail = count % 100
    if 11 <= tail <= 14:
        return "операций"
    last = count % 10
    if last == 1:
        return "операцию"
    if 2 <= last <= 4:
        return "операции"
    return "операций"


# --------------------------------------------------------------------------
# Пост-обработка
# --------------------------------------------------------------------------


def post_writeoff_claims(ctx: Ctx, now: datetime) -> None:
    """Долг компании, не закрытый к дедлайну, становится моей тратой.

    Списание датируется дедлайном, а не днём ужина: уже показанные
    пользователю периоды не переписываются задним числом.
    """
    claims = ctx.session.scalars(
        select(Claim).where(
            Claim.user_id == ctx.user_id,
            Claim.kind == ClaimKind.SHARED_BILL,
            Claim.status == ClaimStatus.OPEN,
        )
    ).all()
    for claim in claims:
        if claim.deadline_at is None or claim.deadline_at > now or claim.outstanding <= 0:
            continue
        amount = claim.outstanding
        source = ctx.session.get(Operation, claim.source_operation_id)
        category = source.category_code if source else cats.OTHER
        op = ctx.new_op(
            kind=OperationKind.ADJUSTMENT,
            occurred_at=claim.deadline_at,
            title=f"Не вернули: {claim.label}",
            amount=amount,
            legs=[],
            rule="claim_writeoff",
            category=category,
            source=OperationSource.SYSTEM,
            explanation=(
                f"Прошло {ctx.settings.shared_bill_deadline_days} дн., "
                f"{amount / 100:.0f} ₽ так и не вернули — сумма признана моей тратой."
            ),
        )
        op.claim_id = claim.id
        build.claim_writeoff(
            ctx.session, ctx.reg, op,
            receivable=claim.account, amount=amount, category=category, at=claim.deadline_at,
        )
        claim.status = ClaimStatus.WRITTEN_OFF
        claim.closed_at = claim.deadline_at


def post_unallocated_cash(ctx: Ctx, now: datetime) -> None:
    """Наличные, про которые так и не рассказали, уходят в отдельную категорию.

    FIFO: сначала объясняются самые ранние снятия. Остаток снятия,
    пролежавший дольше настроенного срока, списывается датой истечения
    срока — с честной пометкой, что на что ушло, неизвестно.
    """
    horizon = timedelta(days=ctx.settings.cash_unallocated_after_days)
    withdrawals = sorted(
        (op for op in ctx.ops if op.kind == OperationKind.CASH_WITHDRAWAL),
        key=lambda o: o.occurred_at,
    )
    if not withdrawals:
        return
    spends = sorted(
        (op for op in ctx.ops if op.kind == OperationKind.CASH_EXPENSE),
        key=lambda o: o.occurred_at,
    )

    remaining = [[w, w.amount] for w in withdrawals]
    for spend in spends:
        left = spend.amount
        for chunk in remaining:
            if left <= 0:
                break
            if chunk[1] <= 0 or chunk[0].occurred_at > spend.occurred_at:
                continue
            take = min(left, chunk[1])
            chunk[1] -= take
            left -= take

    for withdrawal, unspent in remaining:
        if unspent <= 0:
            continue
        anchor = withdrawal.extra.get("anchor") or f"op:{withdrawal.id}"
        due = withdrawal.occurred_at + horizon

        told = ctx.decision(anchor, DecisionKind.CASH_ALLOCATION)
        if told:
            # Пользователь сказал, на что ушли наличные — это трата, и
            # категория у неё та, которую он назвал.
            category = told.get("category") or cats.OTHER
            op = ctx.new_op(
                kind=OperationKind.CASH_EXPENSE,
                occurred_at=withdrawal.occurred_at,
                title=f"Наличные: {cats.title(category)}",
                amount=unspent,
                legs=[],
                rule="cash_allocation",
                category=category,
                source=OperationSource.MANUAL,
                explanation="Вы указали, на что ушли снятые наличные.",
                # Своих legs у этой операции нет, а якорь нужен: по нему
                # ответ своими словами находит её и подписывает.
                extra={"anchor": anchor},
            )
            op.status = OperationStatus.CONFIRMED
            op.explained = True
            build.cash_expense(
                ctx.session, ctx.reg, op, amount=unspent, category=category
            )
            continue

        # Срок признания тратой не закрывает возможность объяснить её.
        # Иначе категория «Наличные без назначения» остаётся в метрике,
        # когда вопрос уже исчез из очереди.
        if due <= now or now - withdrawal.occurred_at >= timedelta(days=3):
            ctx.ask(
                ReviewKind.CASH_SPENT_ON,
                (
                    f"Из снятых {_rub(withdrawal.amount)} от "
                    f"{withdrawal.occurred_at:%d.%m} не объяснено "
                    f"{_rub(unspent)}. На что ушли?"
                ),
                _category_options(),
                anchor=anchor,
                priority=_priority(unspent, 85),
                operation=withdrawal,
            )
        if due > now:
            continue  # ещё есть время рассказать
        op = ctx.new_op(
            kind=OperationKind.ADJUSTMENT,
            occurred_at=due,
            title="Наличные без назначения",
            amount=unspent,
            legs=[],
            rule="cash_unallocated",
            category=cats.CASH_UNALLOCATED,
            source=OperationSource.SYSTEM,
            explanation=(
                f"Из снятия {withdrawal.occurred_at:%d.%m} осталось "
                f"{unspent / 100:.0f} ₽, про которые ничего не сказано. "
                f"Деньги потрачены, но на что — неизвестно."
            ),
        )
        build.unallocated_cash(ctx.session, ctx.reg, op, amount=unspent, at=due)


# Категории, в которых регулярность — свойство жизни, а не подписки.
# Продукты покупают каждую неделю, но «Пятёрочка» не подписка, и предлагать
# её отменить было бы бессмысленным советом.
NEVER_SUBSCRIPTION = frozenset(
    {"groceries", "cafe", "delivery", "taxi", "housing", "health", "car", "clothes",
     "beauty", "gifts", "kids", "travel", "taxes", cats.OTHER, cats.CASH_UNALLOCATED}
)
# Потолок суммы для подписки: аренда за 72 000 повторяется каждый месяц, но
# это не то, что человек забыл отключить.
SUBSCRIPTION_MAX = 1_000_000  # 10 000 ₽


def post_subscriptions(ctx: Ctx) -> None:
    """Находит регулярные списания: тот же получатель, та же сумма, тот же ритм."""
    buckets: dict[tuple[int | None, int], list[Operation]] = {}
    for op in ctx.ops:
        if op.kind != OperationKind.EXPENSE or op.counterparty_id is None:
            continue
        if op.category_code in NEVER_SUBSCRIPTION or op.amount > SUBSCRIPTION_MAX:
            continue
        bucket = (op.counterparty_id, round(op.amount, -2))  # с точностью до рубля
        buckets.setdefault(bucket, []).append(op)

    for (cp_id, _), group in buckets.items():
        if len(group) < 3:
            continue
        group.sort(key=lambda o: o.occurred_at)
        gaps = [
            (b.occurred_at - a.occurred_at).days for a, b in zip(group, group[1:], strict=False)
        ]
        if not gaps:
            continue
        avg = sum(gaps) / len(gaps)
        period = next(
            (p for p in (7, 14, 30, 31, 90, 365) if abs(avg - p) <= max(3, p * 0.15)), None
        )
        if period is None:
            continue
        cp = ctx.session.get(Counterparty, cp_id)
        ctx.session.add(
            Subscription(
                user_id=ctx.user_id,
                counterparty_id=cp_id,
                label=cp.display_name if cp else "Регулярный платёж",
                amount=group[-1].amount,
                period_days=period,
                occurrences=len(group),
                first_seen=group[0].occurred_at,
                last_seen=group[-1].occurred_at,
                category_code=group[-1].category_code,
            )
        )


def post_places(ctx: Ctx) -> None:
    """Ставит на траты названия, которые человек дал местам.

    Работает поверх готовых операций, как и ответ своими словами: правило
    меняет название и контрагента, но не сумму и не проводки — деньги оно
    не трогает вовсе. Категорию оно тоже здесь не меняет: она уже учтена
    при обогащении, до того как построились проводки, иначе название
    разошлось бы со счётом расхода.

    Контрагента переписываем не для красоты: в отчётах имя продавца берётся
    из контрагента, и без этого «InterFood», «INTERFOOD 12» и «Интерфуд»
    остались бы тремя строками в разрезе по продавцам, хотя человек назвал
    их одним местом.
    """
    rules = places.user_places(ctx.session, ctx.user_id)
    if not rules:
        return
    row_by_op: dict[int, RawTransaction] = {}
    for row in ctx.rows:
        if row.operation_id is not None:
            row_by_op.setdefault(row.operation_id, row)

    for op in ctx.ops:
        if op.kind not in (OperationKind.EXPENSE, OperationKind.CASH_EXPENSE):
            continue
        row = row_by_op.get(op.id)
        if row is None:
            continue
        merchant = clean_merchant(row.counterparty_raw or row.description)
        rule = places.match(rules, merchant)
        if rule is None:
            continue
        was = op.title
        op.title = rule.title[:255]
        op.counterparty_id = ctx.reg.counterparty(rule.title, CounterpartyKind.MERCHANT).id
        # Ключ правила — в extra: по нему считается, сколько операций носит
        # это имя, и Mini App понимает, какое место открывать на правку.
        op.extra = {**op.extra, "place": rule.pattern}
        op.rule = "my_place"
        # Человек назвал это место — значит, знает, что это за деньги. Даже
        # если категория не назначена, в неясные такую трату записывать
        # больше нельзя: ровно так же работает ответ своими словами.
        op.explained = True
        why = f"«{was}» — это ваше место «{rule.title}», вы его так назвали."
        if rule.category_code:
            why += f" Категория «{cats.title(rule.category_code)}» — из того же правила."
        op.explanation = why


def post_free_text(ctx: Ctx) -> None:
    """Подписывает операции словами человека, когда он ответил своими словами.

    Работает поверх готовых операций, а не вместо правил: классификацию
    ведёт обычное решение (категория, вид операции, распределение наличных),
    а здесь операция получает название и объяснение от человека и метку
    «объяснено». Метка важна не для красоты: без категории такая трата
    иначе навсегда осталась бы в неясных, хотя смысл денег известен.
    """
    for op in ctx.ops:
        anchor = op.extra.get("anchor")
        told = ctx.decision(anchor, DecisionKind.FREE_TEXT) if anchor else None
        text = str((told or {}).get("text") or "").strip()
        if not text:
            continue
        if op.kind == OperationKind.CASH_WITHDRAWAL:
            # У снятия и у траты этими наличными общий якорь, но человек
            # объясняет не поход к банкомату, а куда деньги ушли потом.
            continue
        was = op.title
        op.title = (text[0].upper() + text[1:])[:255]
        op.explanation = (
            f"Вы объяснили своими словами: «{text}». Было: «{was}». "
            + (
                f"Категория «{cats.title(op.category_code)}» — из ваших слов."
                if told.get("category")
                else "Категория из слов не вычиталась, но операция больше не "
                "считается непонятной: смысл денег вы назвали."
            )
        )
        op.rule = "user_text"
        op.confidence = 1.0
        op.status = OperationStatus.CONFIRMED
        op.explained = True


def post_unclear_places(ctx: Ctx) -> None:
    """Спрашивает про неопознанные места — одним вопросом на продавца.

    Метрика осознанности говорит «этих денег я не понимаю». Пока про них
    нельзя было спросить, она была приговором: четыре похода в «Bejs-Klab»
    по 300–1100 ₽ висели в «Прочем», каждый по отдельности не дотягивал до
    порога крупной траты, и способа объяснить их у человека не было вовсе.
    Метрика, которую нельзя починить действием, хуже отсутствия метрики.

    Группировка по месту, а не по операции, — не экономия запросов, а
    условие того, что на вопрос вообще ответят: девяносто девять одинаковых
    вопросов человек закрывает не ответами, а выходом из приложения. Один
    ответ становится правилом для продавца и объясняет все его траты разом,
    включая будущие, — см. app/classify/places.py.

    Идёт последним из пост-обработок: к этому моменту уже применены и мои
    места, и ответы своими словами, и спрашивать остаётся только про то,
    что действительно осталось непонятным.
    """
    groups: dict[str, dict] = {}
    for op in ctx.ops:
        if op.kind not in (OperationKind.EXPENSE, OperationKind.CASH_EXPENSE):
            continue
        if op.explained or (op.category_code or cats.OTHER) != cats.OTHER:
            continue
        anchor = op.extra.get("anchor")
        if anchor and ctx.decision(anchor, DecisionKind.SET_CATEGORY):
            continue  # про эту операцию человек уже сказал отдельно
        cp = ctx.session.get(Counterparty, op.counterparty_id) if op.counterparty_id else None
        if cp is None or cp.kind == CounterpartyKind.PERSON:
            # У строки без продавца нет ключа для общего правила по месту.
            # Дать ей уйти в «Прочее» без вопроса означало бы ту же дыру,
            # что и порог по мелким покупкам.
            if anchor and not any(ask.anchor == anchor for ask in ctx.asks):
                ctx.ask(
                    ReviewKind.CATEGORY_UNCLEAR,
                    f"{op.title} — {_rub(op.amount)}. Что это за трата?",
                    _category_options(),
                    anchor=anchor,
                    priority=_priority(op.amount, 70),
                    operation=op,
                )
            continue
        # Ключ — тот же, которым «мои места» считают два написания одним
        # местом: иначе «InterFood» и «INTERFOOD 12» дали бы два вопроса.
        key = places.place_key(cp.display_name)
        if not key:
            if anchor and not any(ask.anchor == anchor for ask in ctx.asks):
                ctx.ask(
                    ReviewKind.CATEGORY_UNCLEAR,
                    f"{op.title} — {_rub(op.amount)}. Что это за трата?",
                    _category_options(),
                    anchor=anchor,
                    priority=_priority(op.amount, 70),
                    operation=op,
                )
            continue
        slot = groups.setdefault(
            key, {"name": cp.display_name, "pattern": cp.norm_key, "amount": 0, "ops": []}
        )
        slot["amount"] += op.amount
        slot["ops"].append(op)

    for slot in groups.values():
        count = len(slot["ops"])
        ctx.ask(
            ReviewKind.UNKNOWN_PLACE,
            (
                f"{slot['name']} — {_rub(slot['amount'])} за {count} {_times(count)}. "
                f"Что это за место?"
            ),
            _place_options(),
            # Якорь не сырая строка, а место: вопрос про всех сразу, и
            # ответ на него тоже про всех сразу.
            anchor=f"place:{slot['pattern']}",
            priority=_priority(slot["amount"], 80),
            operation=slot["ops"][0],
        )


# --------------------------------------------------------------------------
# Обязательства
# --------------------------------------------------------------------------


def _open_claims(ctx: Ctx, counterparty_id: int | None, kind: ClaimKind) -> list[Claim]:
    if counterparty_id is None:
        return []
    claims = ctx.session.scalars(
        select(Claim)
        .where(
            Claim.user_id == ctx.user_id,
            Claim.kind == kind,
            Claim.counterparty_id == counterparty_id,
            Claim.status == ClaimStatus.OPEN,
        )
        .order_by(Claim.opened_at)
    ).all()
    return [c for c in claims if c.outstanding > 0]


def _open_claim(ctx: Ctx, counterparty_id: int | None, kind: ClaimKind) -> Claim | None:
    claims = _open_claims(ctx, counterparty_id, kind)
    return claims[0] if claims else None


def _settle(ctx: Ctx, claim: Claim, op: Operation, amount: Money) -> None:
    claim.settled_amount += amount
    ctx.session.add(
        ClaimSettlement(claim_id=claim.id, operation_id=op.id, amount=amount, at=op.occurred_at)
    )
    if claim.outstanding <= 0:
        claim.status = ClaimStatus.SETTLED
        claim.closed_at = op.occurred_at
    ctx.session.flush()


def _rub(kopecks: Money) -> str:
    """Копейки -> «1 600 ₽» с узким пробелом."""
    from app.models import fmt_money  # noqa: PLC0415

    return fmt_money(abs(kopecks))


def _purpose_note(row: RawTransaction) -> str:
    """Человеческая часть назначения платежа, без служебных слов банка.

    В описании лежит «Перевод от Иван П. возврат части долга» — имя и слово
    «перевод» пользователь и так видит, показывать их второй раз незачем.
    """
    text = re.sub(
        r"\b(перевод\w*|от|для|сбп|по\s+номеру\s+телефона|с\s+карты|на\s+карту)\b",
        " ",
        row.description or "",
        flags=re.IGNORECASE,
    )
    text = _PERSON_IN_TEXT.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .,;:-")
    return text if len(text) >= 3 else ""


def _priority(amount: Money, weight: int) -> int:
    """Приоритет вопроса = во сколько он обходится точности отчёта."""
    return weight * 1000 + min(999, amount // 100_000)


# --------------------------------------------------------------------------
# Оркестрация
# --------------------------------------------------------------------------

@dataclass(slots=True)
class ClassifyReport:
    operations: int
    questions: int
    unresolved_amount: Money


def reclassify(session: Session, user_id: int, *, now: datetime | None = None) -> ClassifyReport:
    """Полный пересчёт. Идемпотентен: повторный вызов даёт тот же результат."""
    now = now or datetime.now()
    user = session.get(User, user_id)
    if user is None:
        raise ValueError(f"нет пользователя {user_id}")

    _wipe_derived(session, user_id)
    # _wipe_derived сбрасывает operation_id напрямую в SQL. Если строка уже
    # загружена в эту Session, её старый id останется в памяти; при повторном
    # использовании id ORM сочтёт присваивание неизменённым и не запишет связь.
    session.expire_all()

    rows = list(
        session.scalars(
            select(RawTransaction)
            .where(RawTransaction.user_id == user_id)
            .order_by(RawTransaction.booked_at, RawTransaction.id)
        ).all()
    )
    reg = AccountRegistry(session, user_id)
    enrich(session, reg, rows)
    session.flush()

    decisions = {
        (d.anchor_hash, d.kind): d.payload
        for d in session.scalars(select(Decision).where(Decision.user_id == user_id)).all()
    }
    ignored = {a for (a, k) in decisions if k == DecisionKind.IGNORE}
    rows = [r for r in rows if r.dedup_hash not in ignored]

    own = {
        a.id
        for a in session.scalars(
            select(Account).where(
                Account.user_id == user_id,
                Account.kind.in_(
                    [
                        AccountKind.CARD.value,
                        AccountKind.CASH.value,
                        AccountKind.WALLET.value,
                        AccountKind.DEPOSIT.value,
                    ]
                ),
            )
        ).all()
    }

    ctx = Ctx(
        session=session,
        user=user,
        reg=reg,
        settings=get_settings(),
        rows=rows,
        own_ids=own,
        phone_rules=identities(session, user_id),
        decisions=decisions,
    )

    # Порядок строго значим: склейка нескольких строк в одно событие идёт
    # раньше толкования одиночных строк.
    rule_self_transfers(ctx)      # склейка двух строк в одно событие
    rule_own_accounts_by_text(ctx)  # выписка сама говорит «между своими счетами»
    rule_atm(ctx)                 # снятие наличных
    rule_wallet_topup_alone(ctx)  # пополнение кошелька без пары в выписках
    rule_borrow_in(ctx)           # мне дали в долг — это обязательство, не доход
    rule_debt_out(ctx)            # исходящее физлицу: долг, погашение, подарок
    rule_fallback_expenses(ctx)   # остальные траты — нужны следующим трём правилам
    rule_debt_in(ctx)             # возвраты моих выдач, включая частичные
    rule_refunds(ctx)             # возвраты товара, привязанные к покупке
    rule_shared_bill(ctx)         # оплата за компанию, распознанная задним числом
    rule_unknown_incoming(ctx)    # поступление от человека без объяснения
    rule_fallback_income(ctx)     # всё оставшееся входящее

    post_writeoff_claims(ctx, now)
    post_unallocated_cash(ctx, now)
    post_places(ctx)        # мои места: одно имя на все написания продавца
    post_subscriptions(ctx)
    post_free_text(ctx)     # ответы своими словами: имя и пометка «объяснено»
    post_unclear_places(ctx)  # спрашиваем про то, что так и осталось неясным

    _persist_questions(ctx)
    session.flush()

    unresolved = sum(
        op.amount for op in ctx.ops if op.kind == OperationKind.UNKNOWN
    )
    return ClassifyReport(
        operations=len(ctx.ops),
        questions=len([a for a in ctx.asks]),
        unresolved_amount=unresolved,
    )


def _wipe_derived(session: Session, user_id: int) -> None:
    """Сносит всё производное. Сырые данные и решения пользователя остаются."""
    session.execute(
        RawTransaction.__table__.update()
        .where(RawTransaction.user_id == user_id)
        .values(operation_id=None)
    )
    op_ids = select(Operation.id).where(Operation.user_id == user_id)
    session.execute(delete(ClaimSettlement).where(ClaimSettlement.operation_id.in_(op_ids)))
    session.execute(delete(Posting).where(Posting.user_id == user_id))
    session.execute(delete(ReviewItem).where(ReviewItem.user_id == user_id))
    session.execute(delete(Subscription).where(Subscription.user_id == user_id))
    session.execute(
        Operation.__table__.update()
        .where(Operation.user_id == user_id)
        .values(claim_id=None, parent_id=None)
    )
    session.execute(delete(Claim).where(Claim.user_id == user_id))
    session.execute(delete(Operation).where(Operation.user_id == user_id))
    session.flush()


def _persist_questions(ctx: Ctx) -> None:
    """Сохраняет вопросы, на которые ещё нет ответа."""
    answered_by = {
        ReviewKind.INCOMING_UNKNOWN: (DecisionKind.SET_KIND,),
        ReviewKind.OUTGOING_TO_PERSON: (DecisionKind.SET_KIND,),
        ReviewKind.SHARED_BILL_HEADS: (DecisionKind.PARTICIPANTS,),
        ReviewKind.IS_SELF_TRANSFER: (
            DecisionKind.FORCE_TRANSFER, DecisionKind.NOT_TRANSFER,
        ),
        ReviewKind.CASH_SPENT_ON: (DecisionKind.CASH_ALLOCATION,),
        ReviewKind.CATEGORY_UNCLEAR: (DecisionKind.SET_CATEGORY,),
        ReviewKind.LARGE_UNCATEGORIZED: (DecisionKind.SET_CATEGORY,),
    }
    for ask in ctx.asks:
        if (ask.anchor, DecisionKind.FREE_TEXT) in ctx.decisions:
            continue
        if any((ask.anchor, kind) in ctx.decisions for kind in answered_by.get(ask.kind, ())):
            continue
        ctx.session.add(
            ReviewItem(
                user_id=ctx.user_id,
                operation_id=ask.operation.id if ask.operation else None,
                anchor_hash=ask.anchor,
                kind=ask.kind,
                status=ReviewStatus.OPEN,
                question=ask.question,
                options=ask.options,
                priority=ask.priority,
            )
        )
