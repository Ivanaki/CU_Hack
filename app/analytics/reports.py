"""Отчёты за период. Считаются строго по проводкам.

Ни одна цифра здесь не берётся из Operation.amount: только суммы проводок
по расходным и доходным счетам внутри интервала. Отсюда и сходимость
периодов — недельные срезы складываются в месячный автоматически, потому
что это одно и то же множество проводок, разрезанное по дате.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.periods import (
    RU_WEEKDAYS,
    RU_WEEKDAYS_SHORT,
    Period,
    split_auto,
    split_into_days,
    split_into_months,
    split_into_weeks,
)
from app.classify import categories as cats
from app.models import (
    Account,
    AccountKind,
    Claim,
    ClaimKind,
    ClaimStatus,
    Counterparty,
    Money,
    Operation,
    OperationKind,
    Posting,
    Subscription,
)


@dataclass(slots=True)
class CategoryLine:
    code: str
    title: str
    emoji: str
    amount: Money
    share: float
    essential: bool
    prev_amount: Money = 0

    @property
    def delta(self) -> Money:
        return self.amount - self.prev_amount

    @property
    def delta_pct(self) -> float | None:
        if not self.prev_amount:
            return None
        return (self.amount - self.prev_amount) / self.prev_amount * 100


@dataclass(slots=True)
class Awareness:
    """Процент осознанности: доля оборота, смысл которого системе понятен.

    Не «сколько трат человек записал руками» — это была бы метрика
    дисциплины. Здесь метрика качества данных: какая часть денег имеет
    установленный смысл и категорию. Всё остальное — честно признанное
    незнание: неразобранные поступления, траты без категории и наличные,
    про которые никто ничего не сказал.
    """

    total_volume: Money
    unknown_volume: Money
    uncategorized_volume: Money
    unallocated_cash: Money

    @property
    def unclear_volume(self) -> Money:
        return self.unknown_volume + self.uncategorized_volume + self.unallocated_cash

    @property
    def score(self) -> float:
        """Доля понятного в деньгах, которые ушли или чьё направление неясно.

        Зарплата в знаменатель не входит: она большая, стабильная и понятная,
        и от неё метрика перестала бы что-либо показывать — 300 тысяч дохода
        разбавили бы любую неясность до неразличимости.
        """
        if self.total_volume <= 0:
            return 1.0
        return max(0.0, 1.0 - self.unclear_volume / self.total_volume)

    @property
    def percent(self) -> int:
        """Целые проценты, но без вранья округлением.

        Пока хоть рубль не разобран, показывать 100% нельзя: вся ценность
        метрики в том, что ей можно верить. 99% с открытым вопросом честнее,
        чем круглая сотня, за которой прячется неизвестность.
        """
        value = int(round(self.score * 100))
        if self.unclear_volume > 0:
            return min(99, value)
        return value


@dataclass(slots=True)
class Forecast:
    """Сколько выйдет к концу периода, если темп не изменится.

    Считается по уже прошедшим дням периода, а не по всей его длине:
    иначе третьего числа прогноз показывал бы почти ноль. Регулярные
    платежи, которые в этом месяце уже прошли (аренда, подписки),
    исключаются из темпа — они не повторятся, и без этого прогноз
    улетал бы в космос.
    """

    projected: Money
    already_spent: Money
    days_passed: int
    days_total: int
    daily_rate: Money

    @property
    def is_meaningful(self) -> bool:
        # В первые дни периода темп ещё ни о чём не говорит.
        return self.days_passed >= 3 and self.days_passed < self.days_total

    @property
    def remaining(self) -> Money:
        return max(0, self.projected - self.already_spent)


@dataclass(slots=True)
class PeriodReport:
    period: Period
    spend_total: Money
    income_total: Money
    categories: list[CategoryLine]
    awareness: Awareness
    essential_total: Money
    optional_total: Money
    operations_count: int
    open_questions: int
    prev_spend_total: Money = 0
    balances: dict[str, Money] = field(default_factory=dict)
    debts_owed_to_me: Money = 0
    debts_i_owe: Money = 0
    forecast: Forecast | None = None

    @property
    def net(self) -> Money:
        return self.income_total - self.spend_total

    @property
    def spend_delta(self) -> Money:
        return self.spend_total - self.prev_spend_total

    @property
    def avg_per_day(self) -> Money:
        days = max(1, self.period.days)
        return self.spend_total // days


# --- базовые запросы -------------------------------------------------------


def spend_by_category(session: Session, user_id: int, period: Period) -> dict[str, Money]:
    rows = session.execute(
        select(Posting.category_code, func.sum(Posting.amount))
        .where(
            Posting.user_id == user_id,
            Posting.account_kind == AccountKind.EXPENSE.value,
            Posting.at >= period.start,
            Posting.at < period.end,
        )
        .group_by(Posting.category_code)
    ).all()
    return {code or cats.OTHER: int(total) for code, total in rows if total}


def spend_total(session: Session, user_id: int, period: Period) -> Money:
    return int(
        session.scalar(
            select(func.coalesce(func.sum(Posting.amount), 0)).where(
                Posting.user_id == user_id,
                Posting.account_kind == AccountKind.EXPENSE.value,
                Posting.at >= period.start,
                Posting.at < period.end,
            )
        )
        or 0
    )


def income_total(session: Session, user_id: int, period: Period) -> Money:
    # Доходные счета кредитовые: в книге они лежат со знаком минус.
    return -int(
        session.scalar(
            select(func.coalesce(func.sum(Posting.amount), 0)).where(
                Posting.user_id == user_id,
                Posting.account_kind == AccountKind.INCOME.value,
                Posting.at >= period.start,
                Posting.at < period.end,
            )
        )
        or 0
    )


def account_balances(
    session: Session, user_id: int, as_of: Period | None = None
) -> dict[str, Money]:
    """Остатки по собственным счетам: карты, наличные, кошельки."""
    q = (
        select(Account.name, func.coalesce(func.sum(Posting.amount), 0))
        .join(Posting, Posting.account_id == Account.id)
        .where(
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
        .group_by(Account.name)
    )
    if as_of is not None:
        q = q.where(Posting.at < as_of.end)
    return {name: int(total) for name, total in session.execute(q).all()}


def claim_totals(session: Session, user_id: int) -> tuple[Money, Money]:
    """Сколько должны мне и сколько должен я, по открытым обязательствам."""
    claims = session.scalars(
        select(Claim).where(Claim.user_id == user_id, Claim.status == ClaimStatus.OPEN)
    ).all()
    to_me = sum(c.outstanding for c in claims if c.kind != ClaimKind.DEBT_BORROWED)
    by_me = sum(c.outstanding for c in claims if c.kind == ClaimKind.DEBT_BORROWED)
    return to_me, by_me


def explained_spend(session: Session, user_id: int, period: Period) -> Money:
    """Траты без категории, которые человек объяснил своими словами."""
    return int(
        session.scalar(
            select(func.coalesce(func.sum(Posting.amount), 0))
            .join(Operation, Operation.id == Posting.operation_id)
            .where(
                Posting.user_id == user_id,
                Posting.account_kind == AccountKind.EXPENSE.value,
                Posting.category_code == cats.OTHER,
                Operation.explained.is_(True),
                Posting.at >= period.start,
                Posting.at < period.end,
            )
        )
        or 0
    )


def awareness(session: Session, user_id: int, period: Period) -> Awareness:
    unknown = int(
        session.scalar(
            select(func.coalesce(func.sum(Operation.amount), 0)).where(
                Operation.user_id == user_id,
                Operation.kind == OperationKind.UNKNOWN,
                Operation.occurred_at >= period.start,
                Operation.occurred_at < period.end,
            )
        )
        or 0
    )
    by_cat = spend_by_category(session, user_id, period)
    spent = sum(by_cat.values())
    # «Прочее», про которое человек рассказал своими словами, из неясного
    # вычитается: словарь категорию не нашёл, но смысл денег человек назвал,
    # и держать такую трату в незнании было бы враньём в другую сторону.
    uncategorized = by_cat.get(cats.OTHER, 0) - explained_spend(session, user_id, period)
    return Awareness(
        # Знаменатель — потраченное плюс то, про что даже направление неясно.
        total_volume=spent + unknown,
        unknown_volume=unknown,
        uncategorized_volume=max(0, uncategorized),
        unallocated_cash=max(0, by_cat.get(cats.CASH_UNALLOCATED, 0)),
    )


def forecast(
    session: Session, user_id: int, period: Period, *, now: datetime | None = None
) -> Forecast | None:
    """Прогноз трат до конца периода по текущему темпу.

    Из темпа исключаются крупные регулярные платежи, которые в этом периоде
    уже прошли: аренда списывается шестого числа один раз, и если считать
    её частью дневного темпа, прогноз получится втрое завышенным.
    """
    now = now or datetime.now()
    if not (period.start <= now < period.end):
        return None  # период закрыт или ещё не начался — прогнозировать нечего

    days_passed = max(1, (now - period.start).days + 1)
    days_total = max(1, period.days)
    spent = spend_total(session, user_id, period)

    once_per_period = int(
        session.scalar(
            select(func.coalesce(func.sum(Posting.amount), 0)).where(
                Posting.user_id == user_id,
                Posting.account_kind == AccountKind.EXPENSE.value,
                Posting.category_code.in_(["housing", "subscriptions", "taxes"]),
                Posting.at >= period.start,
                Posting.at < period.end,
            )
        )
        or 0
    )
    variable = max(0, spent - once_per_period)
    rate = variable // days_passed
    projected = spent + rate * (days_total - days_passed)

    return Forecast(
        projected=projected,
        already_spent=spent,
        days_passed=days_passed,
        days_total=days_total,
        daily_rate=rate,
    )


def open_questions_count(session: Session, user_id: int) -> int:
    from app.models import ReviewItem, ReviewStatus  # noqa: PLC0415

    return int(
        session.scalar(
            select(func.count())
            .select_from(ReviewItem)
            .where(ReviewItem.user_id == user_id, ReviewItem.status == ReviewStatus.OPEN)
        )
        or 0
    )


# --- сводный отчёт ---------------------------------------------------------


def build_report(
    session: Session, user_id: int, period: Period, *, with_comparison: bool = True
) -> PeriodReport:
    current = spend_by_category(session, user_id, period)
    previous = (
        spend_by_category(session, user_id, period.shifted_back()) if with_comparison else {}
    )
    total = sum(current.values())

    lines: list[CategoryLine] = []
    for code, amount in current.items():
        cat = cats.BY_CODE.get(code)
        lines.append(
            CategoryLine(
                code=code,
                title=cats.title(code),
                emoji=cat.emoji if cat else "❔",
                amount=amount,
                share=(amount / total) if total else 0.0,
                essential=cats.is_essential(code),
                prev_amount=previous.get(code, 0),
            )
        )
    lines.sort(key=lambda line: line.amount, reverse=True)

    ops_count = int(
        session.scalar(
            select(func.count())
            .select_from(Operation)
            .where(
                Operation.user_id == user_id,
                Operation.occurred_at >= period.start,
                Operation.occurred_at < period.end,
            )
        )
        or 0
    )
    to_me, by_me = claim_totals(session, user_id)

    return PeriodReport(
        period=period,
        spend_total=total,
        income_total=income_total(session, user_id, period),
        categories=lines,
        awareness=awareness(session, user_id, period),
        essential_total=sum(line.amount for line in lines if line.essential),
        optional_total=sum(line.amount for line in lines if not line.essential),
        operations_count=ops_count,
        open_questions=open_questions_count(session, user_id),
        prev_spend_total=sum(previous.values()),
        balances=account_balances(session, user_id, period),
        debts_owed_to_me=to_me,
        debts_i_owe=by_me,
        forecast=forecast(session, user_id, period),
    )


def weekly_breakdown(
    session: Session, user_id: int, period: Period
) -> list[tuple[Period, Money]]:
    """Недельные срезы месяца. Их сумма равна месячному итогу — проверяется тестом."""
    return [(w, spend_total(session, user_id, w)) for w in split_into_weeks(period)]


@dataclass(slots=True)
class WalletTrace:
    """Куда ушли деньги, заведённые на кошелёк маркетплейса."""

    name: str
    topped_up: Money
    spent_by_category: dict[str, Money]
    balance: Money

    @property
    def spent(self) -> Money:
        return sum(self.spent_by_category.values())


def wallet_traces(session: Session, user_id: int, period: Period) -> list[WalletTrace]:
    """Разбор по всем кошелькам маркетплейсов за период.

    Кейс требует связывать пополнение карты маркетплейса с последующими
    тратами по ней и восстанавливать категории. Связь обеспечивает сам
    счёт кошелька, а это — её видимая часть: сколько завели и на что ушло.
    """
    wallets = session.scalars(
        select(Account).where(
            Account.user_id == user_id, Account.kind == AccountKind.WALLET.value
        )
    ).all()
    out: list[WalletTrace] = []
    for wallet in wallets:
        topped, by_category = wallet_trace(session, user_id, wallet.key, period)
        if not topped and not by_category:
            continue
        balance = int(
            session.scalar(
                select(func.coalesce(func.sum(Posting.amount), 0)).where(
                    Posting.account_id == wallet.id, Posting.at < period.end
                )
            )
            or 0
        )
        out.append(
            WalletTrace(
                name=wallet.name,
                topped_up=topped,
                spent_by_category=by_category,
                balance=balance,
            )
        )
    return out


def wallet_trace(
    session: Session, user_id: int, wallet_key: str, period: Period
) -> tuple[Money, dict[str, Money]]:
    """Куда ушли деньги, заведённые на кошелёк маркетплейса.

    Закрывает пункт кейса про связывание пополнения карты маркетплейса с
    последующими тратами и восстановление категорий: пополнение видно как
    вход, а траты — уже разложенными по категориям.
    """
    wallet = session.scalar(
        select(Account).where(Account.user_id == user_id, Account.key == wallet_key)
    )
    if wallet is None:
        return 0, {}

    topped = int(
        session.scalar(
            select(func.coalesce(func.sum(Posting.amount), 0)).where(
                Posting.account_id == wallet.id,
                Posting.amount > 0,
                Posting.at >= period.start,
                Posting.at < period.end,
            )
        )
        or 0
    )
    spend_ops = select(Posting.operation_id).where(
        Posting.account_id == wallet.id,
        Posting.amount < 0,
        Posting.at >= period.start,
        Posting.at < period.end,
    )
    rows = session.execute(
        select(Posting.category_code, func.sum(Posting.amount))
        .where(
            Posting.user_id == user_id,
            Posting.account_kind == AccountKind.EXPENSE.value,
            Posting.operation_id.in_(spend_ops),
        )
        .group_by(Posting.category_code)
    ).all()
    return topped, {code or cats.OTHER: int(total) for code, total in rows}


# --- срезы периода ---------------------------------------------------------


def slice_breakdown(
    session: Session, user_id: int, period: Period
) -> tuple[str, list[tuple[Period, Money]]]:
    """Срезы периода подходящей длины и траты по каждому.

    Неделя режется днями, месяц — неделями, год — месяцами. Сумма срезов
    равна итогу периода: это то же множество проводок, разрезанное по дате.
    """
    kind, parts = split_auto(period)
    return kind, [(part, spend_total(session, user_id, part)) for part in parts]


# --- продавцы --------------------------------------------------------------


@dataclass(slots=True)
class MerchantLine:
    """Один продавец за период: сколько раз и на сколько."""

    name: str
    counterparty_id: int | None
    amount: Money
    count: int
    category_code: str | None
    share: float = 0.0

    @property
    def average(self) -> Money:
        return self.amount // self.count if self.count else 0


def spend_by_merchant(session: Session, user_id: int, period: Period) -> list[MerchantLine]:
    """Траты по продавцам. Деньги — из проводок, имя — из контрагента.

    Сумма по всем продавцам равна итогу периода: у каждой расходной проводки
    есть операция, а у операции — имя. Поэтому это не «топ по мнению
    системы», а полное разложение той же суммы, у которого есть хвост.

    Возврат в магазин уменьшает сумму именно у этого продавца: проводка
    отрицательная, и это правильно — за период у него потрачено меньше.
    """
    rows = session.execute(
        select(
            Operation.counterparty_id,
            Counterparty.display_name,
            Operation.title,
            Operation.category_code,
            func.sum(Posting.amount),
        )
        .join(Posting, Posting.operation_id == Operation.id)
        .outerjoin(Counterparty, Counterparty.id == Operation.counterparty_id)
        .where(
            Posting.user_id == user_id,
            Posting.account_kind == AccountKind.EXPENSE.value,
            Posting.at >= period.start,
            Posting.at < period.end,
        )
        .group_by(
            Operation.id,
            Operation.counterparty_id,
            Counterparty.display_name,
            Operation.title,
            Operation.category_code,
        )
    ).all()

    buckets: dict[str, dict] = {}
    for cp_id, cp_name, title, category, total in rows:
        amount = int(total or 0)
        name = (cp_name or title or "").strip() or "Без названия"
        key = f"cp:{cp_id}" if cp_id else f"name:{name.lower()}"
        slot = buckets.setdefault(
            key, {"name": name, "cp": cp_id, "amount": 0, "count": 0, "cats": {}}
        )
        slot["amount"] += amount
        slot["count"] += 1
        if category:
            slot["cats"][category] = slot["cats"].get(category, 0) + amount

    grand = sum(slot["amount"] for slot in buckets.values())
    lines = [
        MerchantLine(
            name=slot["name"],
            counterparty_id=slot["cp"],
            amount=slot["amount"],
            count=slot["count"],
            # Категория продавца — та, на которую у него ушло больше денег:
            # в одном и том же магазине берут и продукты, и товары для дома.
            category_code=(
                max(slot["cats"].items(), key=lambda kv: kv[1])[0] if slot["cats"] else None
            ),
            share=(slot["amount"] / grand) if grand else 0.0,
        )
        for slot in buckets.values()
    ]
    lines.sort(key=lambda item: (item.amount, item.count), reverse=True)
    return lines


def top_merchants(
    session: Session, user_id: int, period: Period, *, limit: int = 10
) -> list[MerchantLine]:
    """Первые limit продавцов периода. Остальные никуда не деваются."""
    return spend_by_merchant(session, user_id, period)[:limit]


# --- дни недели ------------------------------------------------------------


@dataclass(slots=True)
class WeekdayLine:
    """Сколько утекает в конкретный день недели за период."""

    index: int  # 0 — понедельник
    title: str
    short: str
    amount: Money
    count: int
    days: int  # сколько таких дней попало в период
    share: float = 0.0

    @property
    def avg_per_day(self) -> Money:
        """Среднее за один такой день."""
        return self.amount // self.days if self.days else 0


def spend_by_weekday(session: Session, user_id: int, period: Period) -> list[WeekdayLine]:
    """Все семь дней недели, включая пустые: у графика должна быть ось.

    Среднее считается делением на число таких дней в периоде, а не на семь:
    в месяце бывает пять пятниц и четыре субботы, и без этого пятница
    оказывается «дороже» просто потому, что её больше.
    """
    rows = session.execute(
        select(Posting.at, Posting.amount, Posting.operation_id).where(
            Posting.user_id == user_id,
            Posting.account_kind == AccountKind.EXPENSE.value,
            Posting.at >= period.start,
            Posting.at < period.end,
        )
    ).all()

    totals = [0] * 7
    ops: list[set[int]] = [set() for _ in range(7)]
    for at, amount, operation_id in rows:
        index = at.weekday()
        totals[index] += int(amount)
        ops[index].add(operation_id)

    days = [0] * 7
    for chunk in split_into_days(period):
        days[chunk.start.weekday()] += 1

    grand = sum(totals)
    return [
        WeekdayLine(
            index=index,
            title=RU_WEEKDAYS[index],
            short=RU_WEEKDAYS_SHORT[index],
            amount=totals[index],
            count=len(ops[index]),
            days=days[index],
            share=(totals[index] / grand) if grand else 0.0,
        )
        for index in range(7)
    ]


def peak_weekday(lines: list[WeekdayLine]) -> WeekdayLine | None:
    """День недели, в который уходит больше всего — по среднему за день."""
    candidates = [line for line in lines if line.days and line.amount > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda line: line.avg_per_day)


# --- средний чек -----------------------------------------------------------


@dataclass(slots=True)
class AverageCheck:
    """Средний чек по категории: сумма, число покупок и самая крупная."""

    code: str
    title: str
    emoji: str
    amount: Money
    count: int
    biggest: Money

    @property
    def average(self) -> Money:
        return self.amount // self.count if self.count else 0


def average_check_by_category(
    session: Session, user_id: int, period: Period
) -> list[AverageCheck]:
    """Чек — это операция, а не проводка.

    Снятие наличных, разложенное на три категории, — не три покупки, а одна
    операция с тремя частями, и в каждой категории она даёт один чек.
    Поэтому сначала группируем по операции, и только потом считаем.
    """
    rows = session.execute(
        select(Posting.category_code, Posting.operation_id, func.sum(Posting.amount))
        .where(
            Posting.user_id == user_id,
            Posting.account_kind == AccountKind.EXPENSE.value,
            Posting.at >= period.start,
            Posting.at < period.end,
        )
        .group_by(Posting.category_code, Posting.operation_id)
    ).all()

    agg: dict[str, list[int]] = {}
    for code, _operation_id, total in rows:
        amount = int(total or 0)
        slot = agg.setdefault(code or cats.OTHER, [0, 0, 0])  # сумма, чеков, самый крупный
        slot[0] += amount
        if amount:
            slot[1] += 1
        slot[2] = max(slot[2], amount)

    # Категория, где покупка и возврат дали в сумме ноль, из списка уходит:
    # так же поступает spend_by_category, и обе таблицы должны состоять из
    # одних и тех же строк, иначе в отчёте появится «Техника — 0 ₽».
    lines = [
        AverageCheck(
            code=code,
            title=cats.title(code),
            emoji=(cats.BY_CODE[code].emoji if code in cats.BY_CODE else "❔"),
            amount=total,
            count=count,
            biggest=biggest,
        )
        for code, (total, count, biggest) in agg.items()
        if total
    ]
    lines.sort(key=lambda line: (line.average, line.amount), reverse=True)
    return lines


# --- подписки --------------------------------------------------------------

# Сколько раз в году списывается платёж с таким ритмом. Словарь, а не
# 365 / period_days: «раз в месяц» — это 12 списаний в год, а не 12,17.
CHARGES_PER_YEAR = {7: 52, 14: 26, 28: 13, 30: 12, 31: 12, 90: 4, 180: 2, 365: 1}


def charges_per_year(period_days: int) -> int:
    return CHARGES_PER_YEAR.get(period_days) or max(1, round(365 / max(1, period_days)))


@dataclass(slots=True)
class SubscriptionLine:
    """Найденное регулярное списание и его цена в месяц и в год."""

    label: str
    category_code: str | None
    amount: Money  # одно списание
    period_days: int
    occurrences: int
    monthly: Money
    yearly: Money
    last_charge_at: datetime
    next_charge_at: datetime
    paid_in_period: Money = 0
    confirmed: bool = False


def subscription_lines(
    session: Session, user_id: int, period: Period | None = None
) -> list[SubscriptionLine]:
    """Подписки списком. Год — это число списаний в году на сумму списания.

    paid_in_period считается по проводкам: сколько этому получателю реально
    ушло за период. Если подписка найдена, а платежей в периоде нет, это
    видно, и «списывается каждый месяц» не превращается в обещание.
    """
    subs = list(
        session.scalars(
            select(Subscription).where(Subscription.user_id == user_id, Subscription.active)
        ).all()
    )
    if not subs:
        return []

    paid: dict[int, Money] = {}
    if period is not None:
        rows = session.execute(
            select(Operation.counterparty_id, func.sum(Posting.amount))
            .join(Posting, Posting.operation_id == Operation.id)
            .where(
                Posting.user_id == user_id,
                Posting.account_kind == AccountKind.EXPENSE.value,
                Posting.at >= period.start,
                Posting.at < period.end,
                Operation.counterparty_id.is_not(None),
            )
            .group_by(Operation.counterparty_id)
        ).all()
        paid = {int(cp_id): int(total or 0) for cp_id, total in rows}

    lines = [
        SubscriptionLine(
            label=sub.label,
            category_code=sub.category_code,
            amount=sub.amount,
            period_days=sub.period_days,
            occurrences=sub.occurrences,
            monthly=sub.amount * charges_per_year(sub.period_days) // 12,
            yearly=sub.amount * charges_per_year(sub.period_days),
            last_charge_at=sub.last_seen,
            next_charge_at=sub.last_seen + timedelta(days=sub.period_days),
            paid_in_period=(paid.get(sub.counterparty_id, 0) if sub.counterparty_id else 0),
            confirmed=sub.confirmed,
        )
        for sub in subs
    ]
    lines.sort(key=lambda line: line.yearly, reverse=True)
    return lines


def subscriptions_cost(lines: list[SubscriptionLine]) -> tuple[Money, Money]:
    """Во сколько обходятся все подписки: в месяц и в год."""
    return sum(line.monthly for line in lines), sum(line.yearly for line in lines)


# --- сравнение с предыдущим периодом ---------------------------------------


@dataclass(slots=True)
class CategoryDelta:
    """Категория в сравнении с предыдущим периодом.

    Отличается от CategoryLine тем, что видит исчезнувшие категории: если в
    сентябре была одежда, а в октябре нет, в отчёте по октябрю такой строки
    нет вовсе — и падение на всю сумму остаётся незамеченным.
    """

    code: str
    title: str
    emoji: str
    essential: bool
    amount: Money
    prev_amount: Money

    @property
    def delta(self) -> Money:
        return self.amount - self.prev_amount

    @property
    def delta_pct(self) -> float | None:
        if not self.prev_amount:
            return None
        return (self.amount - self.prev_amount) / self.prev_amount * 100

    @property
    def direction(self) -> str:
        if not self.prev_amount and self.amount:
            return "new"
        if not self.amount and self.prev_amount:
            return "gone"
        if self.delta > 0:
            return "up"
        if self.delta < 0:
            return "down"
        return "flat"


def category_changes(
    session: Session, user_id: int, period: Period, *, previous: Period | None = None
) -> list[CategoryDelta]:
    """Что выросло и что упало по сравнению с предыдущим периодом."""
    previous = previous or period.shifted_back()
    now_map = spend_by_category(session, user_id, period)
    was_map = spend_by_category(session, user_id, previous)
    lines = [
        CategoryDelta(
            code=code,
            title=cats.title(code),
            emoji=(cats.BY_CODE[code].emoji if code in cats.BY_CODE else "❔"),
            essential=cats.is_essential(code),
            amount=now_map.get(code, 0),
            prev_amount=was_map.get(code, 0),
        )
        for code in set(now_map) | set(was_map)
    ]
    lines.sort(key=lambda line: abs(line.delta), reverse=True)
    return lines


# --- сходимость ------------------------------------------------------------


@dataclass(slots=True)
class AdditivityCheck:
    """Одна проверка вида «части складываются в целое»."""

    parts_name: str
    unit: str
    total: Money
    parts: Money
    parts_count: int

    @property
    def ok(self) -> bool:
        return self.total == self.parts

    @property
    def title(self) -> str:
        return f"{self.parts_name} складываются в {self.unit}"


def additivity_checks(
    session: Session, user_id: int, period: Period, *, with_days: bool | None = None
) -> list[AdditivityCheck]:
    """Предъявляет сходимость на всех уровнях, а не только недели в месяц.

    Каждая часть считается отдельным запросом к проводкам — иначе проверка
    сравнивала бы одно число с самим собой и ничего бы не значила. Дни для
    длинного периода по умолчанию не считаются: для года это 365 запросов,
    и в обычном отчёте они не нужны.
    """
    total = spend_total(session, user_id, period)
    unit = period.unit
    if with_days is None:
        with_days = period.days <= 62

    def by_periods(name: str, parts: list[Period]) -> AdditivityCheck:
        return AdditivityCheck(
            parts_name=name,
            unit=unit,
            total=total,
            parts=sum(spend_total(session, user_id, chunk) for chunk in parts),
            parts_count=len(parts),
        )

    checks: list[AdditivityCheck] = []
    if with_days and period.days > 1:
        checks.append(by_periods("дни", split_into_days(period)))
    if period.days > 7:
        checks.append(by_periods("недели", split_into_weeks(period)))
    if period.days > 45:
        checks.append(by_periods("месяцы", split_into_months(period)))

    by_cat = spend_by_category(session, user_id, period)
    checks.append(AdditivityCheck("категории", unit, total, sum(by_cat.values()), len(by_cat)))
    merchants = spend_by_merchant(session, user_id, period)
    checks.append(
        AdditivityCheck(
            "продавцы", unit, total, sum(line.amount for line in merchants), len(merchants)
        )
    )
    weekdays = spend_by_weekday(session, user_id, period)
    checks.append(
        AdditivityCheck("дни недели", unit, total, sum(line.amount for line in weekdays), 7)
    )
    return checks
