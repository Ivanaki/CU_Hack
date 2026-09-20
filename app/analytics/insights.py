"""Наблюдения и советы по итогам периода.

Все числа считаются из проводок. Языковая модель, если она включена, только
переписывает готовые наблюдения человеческим языком — придумывать суммы ей
не даётся, иначе совет перестанет быть правдой.

Совет считается годным, если он выполним: называет конкретное действие и
конкретную экономию. «Тратьте меньше на еду» сюда не проходит.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.periods import Period
from app.analytics.reports import PeriodReport, subscription_lines, subscriptions_cost
from app.models import (
    AccountKind,
    Counterparty,
    Money,
    Operation,
    OperationKind,
    Posting,
    fmt_money,
)

# Стоимость месячного проездного: единая по Москве, «Тройка» на 30 дней.
# Вынесено константой, потому что это внешний факт, а не наша логика.
TRANSIT_PASS_COST = 307_000  # 3070 ₽


@dataclass(slots=True)
class Insight:
    """Одно наблюдение. saving — сколько денег в месяц оно может вернуть."""

    title: str
    detail: str
    saving: Money = 0
    weight: int = 0

    @property
    def has_saving(self) -> bool:
        return self.saving > 0


def _operations(session: Session, user_id: int, period: Period) -> list[Operation]:
    return list(
        session.scalars(
            select(Operation).where(
                Operation.user_id == user_id,
                Operation.occurred_at >= period.start,
                Operation.occurred_at < period.end,
            )
        ).all()
    )


def taxi_vs_transit(session: Session, user_id: int, period: Period) -> Insight | None:
    """Если такси ездят часто, проездной может оказаться дешевле.

    Ровно тот тип совета, который просит кейс: пользователь мог о нём не
    задуматься, а экономия считается точно.
    """
    ops = [
        op
        for op in _operations(session, user_id, period)
        if op.category_code == "taxi" and op.kind == OperationKind.EXPENSE
    ]
    if len(ops) < 6:
        return None
    total = sum(op.spend_amount for op in ops)
    # Считаем только короткие поездки: длинные проездной не заменяет.
    short = [op for op in ops if op.spend_amount <= 90_000]
    if len(short) < 5:
        return None
    replaceable = sum(op.spend_amount for op in short)
    saving = replaceable - TRANSIT_PASS_COST
    if saving <= 0:
        return None
    return Insight(
        title="Проездной выгоднее такси",
        detail=(
            f"{len(ops)} поездок на {fmt_money(total)}, из них {len(short)} — короткие, "
            f"на {fmt_money(replaceable)}. Месячный проездной стоит "
            f"{fmt_money(TRANSIT_PASS_COST)}: при такой частоте он окупается."
        ),
        saving=saving,
        weight=90,
    )


def subscriptions_load(session: Session, user_id: int) -> Insight | None:
    """Совет и карточка подписок обязаны показывать одно и то же число,
    поэтому и цена в месяц, и цена в год берутся из общего расчёта."""
    subs = subscription_lines(session, user_id)
    if not subs:
        return None
    monthly, yearly = subscriptions_cost(subs)
    if monthly < 30_000:
        return None
    names = ", ".join(line.label for line in subs[:4])
    return Insight(
        title=f"Подписок: {len(subs)}",
        detail=(
            f"{names} — {fmt_money(monthly)} в месяц, {fmt_money(yearly)} в год. "
            f"Списываются сами, поэтому и не замечаются."
        ),
        saving=0,
        weight=70,
    )


def frequent_small_merchant(
    session: Session, user_id: int, period: Period
) -> Insight | None:
    """Мелкие траты у одного продавца, которые по отдельности незаметны."""
    rows = session.execute(
        select(Operation.counterparty_id, func.count(), func.sum(Operation.spend_amount))
        .where(
            Operation.user_id == user_id,
            Operation.kind == OperationKind.EXPENSE,
            Operation.occurred_at >= period.start,
            Operation.occurred_at < period.end,
            Operation.counterparty_id.is_not(None),
        )
        .group_by(Operation.counterparty_id)
        .having(func.count() >= 4)
    ).all()
    best = None
    for cp_id, count, total in rows:
        total = int(total or 0)
        average = total // max(1, count)
        if average > 150_000 or total < 200_000:
            continue  # ищем именно мелкие и частые
        if best is None or total > best[2]:
            best = (cp_id, count, total, average)
    if best is None:
        return None
    cp_id, count, total, average = best
    cp = session.get(Counterparty, cp_id)
    name = cp.display_name if cp else "один продавец"
    return Insight(
        title=f"{name}: {count} раз за период",
        detail=(
            f"Средний чек {fmt_money(average)}, в сумме {fmt_money(total)}. "
            f"По одной покупке это незаметно, за период — ощутимо."
        ),
        saving=0,
        weight=60,
    )


def delivery_vs_groceries(report: PeriodReport) -> Insight | None:
    by_code = {line.code: line.amount for line in report.categories}
    delivery = by_code.get("delivery", 0)
    groceries = by_code.get("groceries", 0)
    if delivery < 200_000 or groceries <= 0:
        return None
    ratio = delivery / groceries
    if ratio < 0.2:
        return None
    return Insight(
        title="Доставка догоняет продукты",
        detail=(
            f"Доставка {fmt_money(delivery)} при тратах на продукты "
            f"{fmt_money(groceries)} — это {ratio * 100:.0f}% сверху. "
            f"Наценка доставки обычно съедает разницу в цене продуктов."
        ),
        saving=int(delivery * 0.3),
        weight=65,
    )


def biggest_growth(report: PeriodReport) -> Insight | None:
    growing = [
        line
        for line in report.categories
        if line.prev_amount > 0 and line.delta > 100_000 and line.delta_pct
    ]
    if not growing:
        return None
    line = max(growing, key=lambda item: item.delta)
    return Insight(
        title=f"{line.title}: рост на {line.delta_pct:.0f}%",
        detail=(
            f"Было {fmt_money(line.prev_amount)}, стало {fmt_money(line.amount)} "
            f"— плюс {fmt_money(line.delta)} к прошлому периоду."
        ),
        weight=80,
    )


def unclear_money(report: PeriodReport) -> Insight | None:
    awareness = report.awareness
    if awareness.unclear_volume < 50_000:
        return None
    parts = []
    if awareness.unknown_volume:
        parts.append(f"неразобранных поступлений на {fmt_money(awareness.unknown_volume)}")
    if awareness.uncategorized_volume:
        parts.append(f"трат без категории на {fmt_money(awareness.uncategorized_volume)}")
    if awareness.unallocated_cash:
        parts.append(f"наличных без назначения на {fmt_money(awareness.unallocated_cash)}")
    return Insight(
        title=f"Осознанность {awareness.percent}%",
        detail=(
            "Непонятно, куда относится " + ", ".join(parts) +
            ". Это единственное, что мешает цифрам быть точными."
        ),
        weight=100 if awareness.percent < 90 else 40,
    )


def optional_share(report: PeriodReport) -> Insight | None:
    if report.spend_total <= 0 or report.optional_total <= 0:
        return None
    share = report.optional_total / report.spend_total
    if share < 0.2:
        return None
    return Insight(
        title=f"Необязательные траты — {share * 100:.0f}%",
        detail=(
            f"{fmt_money(report.optional_total)} из {fmt_money(report.spend_total)}. "
            f"Это та часть, которой можно управлять: остальное — аренда, связь, "
            f"продукты и здоровье."
        ),
        weight=50,
    )


def cash_left(session: Session, user_id: int, period: Period) -> Insight | None:
    balance = int(
        session.scalar(
            select(func.coalesce(func.sum(Posting.amount), 0)).where(
                Posting.user_id == user_id,
                Posting.account_kind == AccountKind.CASH.value,
                Posting.at < period.end,
            )
        )
        or 0
    )
    if balance < 50_000:
        return None
    return Insight(
        title=f"Наличными на руках {fmt_money(balance)}",
        detail=(
            "Пока не сказано, на что они ушли, эти деньги не попадают в статистику. "
            "Одно голосовое — и они разложатся по категориям."
        ),
        weight=55,
    )


def debts_pending(report: PeriodReport) -> Insight | None:
    if report.debts_owed_to_me < 50_000:
        return None
    return Insight(
        title=f"Вам должны {fmt_money(report.debts_owed_to_me)}",
        detail="Эти деньги не считаются тратой и ждут возврата.",
        weight=45,
    )


def collect(session: Session, user_id: int, report: PeriodReport) -> list[Insight]:
    period = report.period
    found = [
        unclear_money(report),
        taxi_vs_transit(session, user_id, period),
        biggest_growth(report),
        subscriptions_load(session, user_id),
        delivery_vs_groceries(report),
        frequent_small_merchant(session, user_id, period),
        cash_left(session, user_id, period),
        optional_share(report),
        debts_pending(report),
    ]
    insights = [item for item in found if item is not None]
    insights.sort(key=lambda item: (item.weight, item.saving), reverse=True)
    return insights


def total_saving(insights: list[Insight]) -> Money:
    return sum(item.saving for item in insights)


# --- необязательная полировка формулировок --------------------------------

_LLM_SYSTEM = (
    "Ты финансовый помощник. Тебе дают готовые наблюдения с точными числами. "
    "Твоя задача — переписать их живым языком, сохранив ВСЕ числа без изменений. "
    "Не добавляй новых чисел и фактов. Не морализируй и не advise экономить вообще. "
    "Каждый пункт — одно-два предложения."
)


def polish(insights: list[Insight], *, llm=None) -> list[Insight]:
    """Переписывает наблюдения живым языком. При недоступности LLM — как есть."""
    from app.llm.client import get_llm  # noqa: PLC0415

    llm = llm or get_llm()
    if not llm.available or not insights:
        return insights

    payload = "\n".join(
        f"{i + 1}. {item.title} — {item.detail}" for i, item in enumerate(insights)
    )
    data = llm.json(
        "Перепиши наблюдения, сохранив все числа.\n\n"
        f"{payload}\n\n"
        'Верни JSON: {"items": [{"n": 1, "title": "...", "detail": "..."}]}',
        system=_LLM_SYSTEM,
        model=None,
    )
    if not isinstance(data, dict):
        return insights
    rewritten = {}
    for item in data.get("items") or []:
        try:
            rewritten[int(item["n"])] = (str(item["title"]), str(item["detail"]))
        except (KeyError, TypeError, ValueError):
            continue
    out = []
    for index, item in enumerate(insights, start=1):
        if index in rewritten:
            title, detail = rewritten[index]
            out.append(Insight(title, detail, item.saving, item.weight))
        else:
            out.append(item)
    return out
