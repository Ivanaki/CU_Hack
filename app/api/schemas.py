"""Формы ответов API. Деньги наружу отдаём и в копейках, и уже отформатированными:
клиенту не нужно повторять логику округления и разделителей разрядов.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.analytics import insights as insights_mod
from app.analytics.reports import PeriodReport
from app.models import Money, Operation, ReviewItem, fmt_money


class MoneyOut(BaseModel):
    kopecks: int
    text: str

    @classmethod
    def of(cls, amount: Money, *, sign: bool = False) -> MoneyOut:
        return cls(kopecks=int(amount), text=fmt_money(amount, sign=sign))


class CategoryOut(BaseModel):
    code: str
    title: str
    emoji: str
    essential: bool
    amount: MoneyOut
    share: float
    prev_amount: MoneyOut
    delta: MoneyOut
    delta_pct: float | None


class AwarenessOut(BaseModel):
    percent: int
    score: float
    unclear: MoneyOut
    unknown: MoneyOut
    uncategorized: MoneyOut
    unallocated_cash: MoneyOut


class PeriodOut(BaseModel):
    label: str
    start: datetime
    end: datetime
    days: int


class QuestionOptionOut(BaseModel):
    code: str
    label: str


class QuestionOut(BaseModel):
    id: int
    kind: str
    question: str
    options: list[QuestionOptionOut]
    explanation: str = ""
    amount: MoneyOut | None = None
    occurred_at: datetime | None = None

    @classmethod
    def of(cls, item: ReviewItem) -> QuestionOut:
        op = item.operation
        return cls(
            id=item.id,
            kind=str(item.kind),
            question=item.question,
            options=[QuestionOptionOut(**o) for o in (item.options or [])],
            explanation=(op.explanation if op else ""),
            amount=MoneyOut.of(op.amount) if op else None,
            occurred_at=(op.occurred_at if op else None),
        )


class InsightOut(BaseModel):
    title: str
    detail: str
    saving: MoneyOut | None = None

    @classmethod
    def of(cls, item: insights_mod.Insight) -> InsightOut:
        return cls(
            title=item.title,
            detail=item.detail,
            saving=MoneyOut.of(item.saving) if item.has_saving else None,
        )


class ForecastOut(BaseModel):
    """Прогноз до конца периода. show=false — периода прошло слишком мало."""

    show: bool
    projected: MoneyOut
    remaining: MoneyOut
    daily_rate: MoneyOut
    days_passed: int
    days_total: int


class WalletTraceOut(BaseModel):
    """Пополнение кошелька маркетплейса и куда с него ушли деньги."""

    name: str
    topped_up: MoneyOut
    spent: MoneyOut
    balance: MoneyOut
    by_category: list[tuple[str, MoneyOut]]


class StreakOut(BaseModel):
    days: int
    best: int
    actions_per_day: float


class WeekSliceOut(BaseModel):
    label: str
    start: datetime
    amount: MoneyOut


class SummaryOut(BaseModel):
    """Всё, что нужно главному экрану Mini App, одним запросом."""

    period: PeriodOut
    spend: MoneyOut
    income: MoneyOut
    net: MoneyOut
    avg_per_day: MoneyOut
    prev_spend: MoneyOut
    spend_delta: MoneyOut
    spend_delta_pct: float | None
    awareness: AwarenessOut
    categories: list[CategoryOut]
    essential: MoneyOut
    optional: MoneyOut
    weeks: list[WeekSliceOut]
    questions: list[QuestionOut]
    open_questions: int
    insights: list[InsightOut]
    streak: StreakOut
    forecast: ForecastOut | None = None
    wallets: list[WalletTraceOut] = []
    balances: dict[str, MoneyOut]
    debts_owed_to_me: MoneyOut
    debts_i_owe: MoneyOut
    operations_count: int


class OperationOut(BaseModel):
    id: int
    occurred_at: datetime
    kind: str
    title: str
    amount: MoneyOut
    spend: MoneyOut
    category: str | None
    counts_as_spending: bool
    explanation: str
    rule: str | None
    confidence: float

    @classmethod
    def of(cls, op: Operation) -> OperationOut:
        return cls(
            id=op.id,
            occurred_at=op.occurred_at,
            kind=str(op.kind),
            title=op.title,
            amount=MoneyOut.of(op.amount),
            spend=MoneyOut.of(op.spend_amount),
            category=op.category_code,
            counts_as_spending=op.spend_amount != 0,
            explanation=op.explanation,
            rule=op.rule,
            confidence=op.confidence,
        )


class AnswerIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class QuickExpenseIn(BaseModel):
    text: str = Field(min_length=1, max_length=200, examples=["кофе 300"])
    occurred_at: datetime | None = None


class IngestResultOut(BaseModel):
    ok: bool
    source: str = ""
    added: int = 0
    duplicates: int = 0
    superseded: int = 0
    operations: int = 0
    questions: int = 0
    unresolved: MoneyOut | None = None
    error: str = ""


def category_out(line) -> CategoryOut:  # noqa: ANN001 — reports.CategoryLine
    return CategoryOut(
        code=line.code,
        title=line.title,
        emoji=line.emoji,
        essential=line.essential,
        amount=MoneyOut.of(line.amount),
        share=round(line.share, 4),
        prev_amount=MoneyOut.of(line.prev_amount),
        delta=MoneyOut.of(line.delta, sign=True),
        delta_pct=(round(line.delta_pct, 1) if line.delta_pct is not None else None),
    )


def forecast_out(report: PeriodReport) -> ForecastOut | None:
    f = report.forecast
    if f is None:
        return None
    return ForecastOut(
        show=f.is_meaningful,
        projected=MoneyOut.of(f.projected),
        remaining=MoneyOut.of(f.remaining),
        daily_rate=MoneyOut.of(f.daily_rate),
        days_passed=f.days_passed,
        days_total=f.days_total,
    )


def wallet_out(trace) -> WalletTraceOut:  # noqa: ANN001 — reports.WalletTrace
    from app.classify import categories as _cats  # noqa: PLC0415

    ordered = sorted(trace.spent_by_category.items(), key=lambda kv: -kv[1])
    return WalletTraceOut(
        name=trace.name,
        topped_up=MoneyOut.of(trace.topped_up),
        spent=MoneyOut.of(trace.spent),
        balance=MoneyOut.of(trace.balance),
        by_category=[(_cats.title(code), MoneyOut.of(amount)) for code, amount in ordered],
    )


def awareness_out(report: PeriodReport) -> AwarenessOut:
    a = report.awareness
    return AwarenessOut(
        percent=a.percent,
        score=round(a.score, 4),
        unclear=MoneyOut.of(a.unclear_volume),
        unknown=MoneyOut.of(a.unknown_volume),
        uncategorized=MoneyOut.of(a.uncategorized_volume),
        unallocated_cash=MoneyOut.of(a.unallocated_cash),
    )
