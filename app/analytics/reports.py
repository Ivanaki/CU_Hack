"""Отчёты за период. Считаются строго по проводкам.

Ни одна цифра здесь не берётся из Operation.amount: только суммы проводок
по расходным и доходным счетам внутри интервала. Отсюда и сходимость
периодов — недельные срезы складываются в месячный автоматически, потому
что это одно и то же множество проводок, разрезанное по дате.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.periods import Period, split_into_weeks
from app.classify import categories as cats
from app.models import (
    Account,
    AccountKind,
    Claim,
    ClaimKind,
    ClaimStatus,
    Money,
    Operation,
    OperationKind,
    Posting,
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
    return Awareness(
        # Знаменатель — потраченное плюс то, про что даже направление неясно.
        total_volume=spent + unknown,
        unknown_volume=unknown,
        uncategorized_volume=max(0, by_cat.get(cats.OTHER, 0)),
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
