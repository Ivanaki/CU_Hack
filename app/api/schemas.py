"""Формы ответов API. Деньги наружу отдаём и в копейках, и уже отформатированными:
клиенту не нужно повторять логику округления и разделителей разрядов.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from app.analytics import insights as insights_mod
from app.analytics.reports import PeriodReport
from app.models import ImportBatch, Money, Operation, ReviewItem, fmt_money
from app.question_context import (
    operation_question_context,
    person_question_label,
    question_date_label,
)


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


class QuestionSourceOut(BaseModel):
    description: str = ""
    phone: str | None = None
    bank: str = ""
    filename: str = ""


# Последняя кнопка любого вопроса. Придуманные заранее варианты покрывают не
# всё («аренда квартиры», «вернул за ремонт»), а «ничего не подходит» — не
# повод оставлять деньги неразобранными. Добавляется здесь, а не в правилах:
# так она появляется разом у всех видов вопросов, включая будущие.
FREE_OPTION = QuestionOptionOut(code="free", label="Своими словами")


class QuestionOut(BaseModel):
    id: int
    kind: str
    status: str
    question: str
    options: list[QuestionOptionOut]
    explanation: str = ""
    amount: MoneyOut | None = None
    occurred_at: datetime | None = None
    occurred_label: str | None = None
    counterparty: str | None = None
    person_label: str | None = None
    transaction_sources: list[QuestionSourceOut] = []

    @classmethod
    def of(cls, item: ReviewItem) -> QuestionOut:
        op = item.operation
        context = operation_question_context(op)
        options = [QuestionOptionOut(**o) for o in (item.options or [])]
        if all(o.code != FREE_OPTION.code for o in options):
            options.append(FREE_OPTION)
        return cls(
            id=item.id,
            kind=str(item.kind),
            status=str(item.status),
            question=item.question,
            options=options,
            explanation=(op.explanation if op else ""),
            amount=MoneyOut.of(op.amount) if op else None,
            occurred_at=(op.occurred_at if op else None),
            occurred_label=question_date_label(op.occurred_at if op else None),
            counterparty=context["counterparty"],
            person_label=(
                person_question_label(context)
                if str(item.kind) in {"incoming_unknown", "outgoing_to_person"}
                else None
            ),
            transaction_sources=[QuestionSourceOut(**source) for source in context["sources"]],
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
    # Чтобы заголовок не был прибит к месяцу: у года конец года, у недели —
    # конец недели. Без этого «до конца месяца» врёт на любом другом периоде.
    period_label: str = ""


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


class SliceOut(BaseModel):
    """Срез периода: для недели — день, для месяца — неделя, для года — месяц."""

    label: str
    start: datetime
    end: datetime
    amount: MoneyOut


class SummaryOut(BaseModel):
    """Всё, что нужно главному экрану Mini App, одним запросом."""

    period: PeriodOut
    # С чем сравниваем. Для календарного месяца это прошлый календарный
    # месяц, поэтому подпись «в августе» можно писать, не гадая.
    prev_period: PeriodOut
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
    # weeks — недельные срезы, как было. Для года их 53, и на экране в 390
    # точек они не помещаются: новый клиент читает slices, где гранулярность
    # подобрана по длине периода, а granularity_title — готовый заголовок.
    weeks: list[WeekSliceOut]
    granularity: str = "week"
    granularity_title: str = "По неделям"
    slices: list[SliceOut] = []
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


class SourceRefOut(BaseModel):
    """Откуда приехала операция: конкретная загрузка и файл за ней.

    Список, а не одна запись: операция склеивается из нескольких сырых
    строк, и перевод себе между двумя банками честно приезжает из двух
    разных файлов.
    """

    batch_id: int
    at: datetime
    source: str
    filename: str = ""
    channel: str = ""
    stored_as: str = ""
    file_url: str | None = None

    @classmethod
    def of(cls, batch: ImportBatch, *, file_url: str | None = None) -> SourceRefOut:
        return cls(
            batch_id=batch.id,
            at=batch.created_at,
            source=batch.source,
            filename=batch.filename,
            channel=batch.channel,
            stored_as=batch.stored_as,
            file_url=file_url,
        )


class OperationOut(BaseModel):
    id: int
    occurred_at: datetime
    kind: str
    title: str
    amount: MoneyOut
    spend: MoneyOut
    category: str | None
    counts_as_spending: bool
    is_pending: bool = False
    explanation: str
    rule: str | None
    confidence: float
    # Пусто, только если операцию не просили сопроводить следом: считать
    # его на каждый список операций — лишний запрос там, где он не нужен.
    sources: list[SourceRefOut] = []
    # Ключ «моего места», если название операции пришло оттуда. Само
    # название уже лежит в title — здесь ключ, чтобы клиент мог открыть это
    # место на правку и показать, что имя дал человек, а не банк.
    place: str | None = None

    @classmethod
    def of(
        cls, op: Operation, *, sources: list[SourceRefOut] | None = None,
        is_pending: bool = False,
    ) -> OperationOut:
        return cls(
            place=(op.extra or {}).get("place"),
            id=op.id,
            occurred_at=op.occurred_at,
            kind=str(op.kind),
            title=op.title,
            amount=MoneyOut.of(op.amount),
            spend=MoneyOut.of(op.spend_amount),
            category=op.category_code,
            counts_as_spending=op.spend_amount != 0,
            is_pending=is_pending,
            explanation=op.explanation,
            rule=op.rule,
            confidence=op.confidence,
            sources=sources or [],
        )


class CategoryChoiceOut(BaseModel):
    code: str
    title: str


class CategoryChangeIn(BaseModel):
    category: str
    scope: str = Field(examples=["operation", "place"])


class ImportOut(BaseModel):
    """Одна загрузка: что приехало и что из этого получилось."""

    id: int
    at: datetime
    source: str
    filename: str = ""
    channel: str = ""
    stored_as: str = ""
    content_type: str = ""
    size: int = 0
    file_url: str | None = None
    rows_total: int = 0
    rows_new: int = 0
    rows_duplicate: int = 0
    operations: int = 0
    questions: int = 0
    period_from: date | None = None
    period_to: date | None = None

    @classmethod
    def of(
        cls,
        batch: ImportBatch,
        *,
        operations: int = 0,
        questions: int = 0,
        file_url: str | None = None,
    ) -> ImportOut:
        return cls(
            id=batch.id,
            at=batch.created_at,
            source=batch.source,
            filename=batch.filename,
            channel=batch.channel,
            stored_as=batch.stored_as,
            content_type=batch.content_type,
            size=batch.size_bytes,
            file_url=file_url,
            rows_total=batch.rows_total,
            rows_new=batch.rows_new,
            rows_duplicate=batch.rows_duplicate,
            operations=operations,
            questions=questions,
            period_from=batch.period_from,
            period_to=batch.period_to,
        )


class ImportDetailOut(BaseModel):
    """Загрузка целиком: сам файл, его операции и его вопросы."""

    batch: ImportOut
    operations: list[OperationOut]
    questions: list[QuestionOut]


class UploadLinkOut(BaseModel):
    """Персональная ссылка для быстрой команды с телефона.

    Токен отдаётся отдельным полем, а не внутри url, намеренно: полный
    адрес со строкой запроса попадает в логи прокси и целиком уезжает в
    чат, если человек решит поделиться ссылкой.
    """

    url: str = Field(description="Куда слать файл — адрес общий для всех")
    token: str = Field(description="Личный секрет: кладётся в заголовок X-Upload-Token")
    curl: str = Field(description="Готовая команда, чтобы проверить прямо сейчас")
    issued_at: datetime | None = Field(default=None, description="Когда ссылка выпущена")


class AnswerIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    # Заполняется только для code="free": что это за операция, словами
    # человека. Для остальных кодов не значит ничего.
    text: str | None = Field(default=None, max_length=200)


class QuickExpenseIn(BaseModel):
    text: str = Field(min_length=1, max_length=200, examples=["кофе 300"])
    occurred_at: datetime | None = None


class PlaceOut(BaseModel):
    """Одно «моё место»: как продавец подписан у человека."""

    pattern: str
    title: str
    category: str | None
    category_title: str
    operations_count: int

    @classmethod
    def of(cls, line) -> PlaceOut:  # noqa: ANN001 — service.PlaceLine
        from app.classify import categories as _cats  # noqa: PLC0415

        return cls(
            pattern=line.pattern,
            title=line.title,
            category=line.category,
            category_title=_cats.title(line.category) if line.category else "",
            operations_count=line.operations_count,
        )


class PlaceIn(BaseModel):
    """Заявка «назвать место по-своему».

    title без min_length осознанно: пустое название — это 400 с внятной
    причиной из общего слоя, а не 422 с разбором схемы. Человек должен
    прочитать, что именно от него хотят, а не «string_too_short».
    """

    merchant: str = Field(max_length=255, examples=["InterFood"])
    title: str = Field(max_length=200, examples=["Узбечка"])
    category: str | None = Field(default=None, examples=["cafe"])


class PhoneIdentityIn(BaseModel):
    phone: str = Field(max_length=32, examples=["+7 900 000-00-01"])
    kind: str = Field(examples=["own", "contact"])
    name: str = Field(default="", max_length=100, examples=["Петя"])


class PhoneIdentityOut(BaseModel):
    phone: str
    kind: str
    name: str


class StatementFileOut(BaseModel):
    """Результат обработки одного файла в пакетной загрузке выписок."""

    filename: str
    ok: bool
    source: str = ""
    added: int = 0
    duplicates: int = 0
    superseded: int = 0
    error: str = ""
    extraction_method: str = ""
    pending_count: int = 0


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
    extraction_method: str = ""  # csv | template | ai
    pending_count: int = 0
    # Номер загрузки: по нему клиент поднимает весь след файла через
    # GET /api/v1/me/imports/{id}, не гадая, какая запись только что создана.
    batch_id: int | None = None
    # Для общей загрузки выписок сохраняем отдельный итог каждого файла,
    # чтобы один неудачный PDF не скрывал результаты остальных.
    files: list[StatementFileOut] = []


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
        period_label=report.period.label,
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


# --------------------------------------------------------------------------
# Разрезы вкладки «Отчёты»: GET /api/v1/me/analytics
# --------------------------------------------------------------------------


class MerchantOut(BaseModel):
    """Продавец за период: сколько раз и на сколько."""

    name: str
    category: str | None
    category_title: str
    amount: MoneyOut
    count: int
    average: MoneyOut
    share: float


class WeekdayOut(BaseModel):
    """День недели. Всегда семь штук подряд, даже пустые: у графика есть ось."""

    index: int
    title: str
    short: str
    amount: MoneyOut
    count: int
    days: int
    avg_per_day: MoneyOut
    share: float


class AverageCheckOut(BaseModel):
    code: str
    title: str
    emoji: str
    amount: MoneyOut
    count: int
    average: MoneyOut
    biggest: MoneyOut


class SubscriptionOut(BaseModel):
    """Регулярное списание: сколько стоит в месяц, в год и когда следующее."""

    label: str
    category: str | None
    category_title: str
    amount: MoneyOut
    period_days: int
    occurrences: int
    monthly: MoneyOut
    yearly: MoneyOut
    last_charge_at: datetime
    next_charge_at: datetime
    paid_in_period: MoneyOut
    confirmed: bool


class CategoryChangeOut(BaseModel):
    """Категория в сравнении с предыдущим периодом.

    direction: up — выросла, down — упала, new — появилась, gone — исчезла.
    """

    code: str
    title: str
    emoji: str
    essential: bool
    amount: MoneyOut
    prev_amount: MoneyOut
    delta: MoneyOut
    delta_pct: float | None
    direction: str


class AdditivityOut(BaseModel):
    """Одна проверка сходимости: «недели складываются в месяц»."""

    parts: str
    title: str
    total: MoneyOut
    parts_total: MoneyOut
    parts_count: int
    ok: bool


class AnalyticsOut(BaseModel):
    """Всё для вкладки «Отчёты» одним запросом."""

    period: PeriodOut
    prev_period: PeriodOut
    granularity: str
    granularity_title: str
    slices: list[SliceOut]
    spend: MoneyOut
    prev_spend: MoneyOut
    spend_delta: MoneyOut
    spend_delta_pct: float | None
    avg_per_day: MoneyOut
    categories: list[CategoryOut]
    top_merchants: list[MerchantOut]
    merchants_total: int
    weekdays: list[WeekdayOut]
    peak_weekday: WeekdayOut | None
    average_checks: list[AverageCheckOut]
    subscriptions: list[SubscriptionOut]
    subscriptions_monthly: MoneyOut
    subscriptions_yearly: MoneyOut
    grew: list[CategoryChangeOut]
    fell: list[CategoryChangeOut]
    additivity: list[AdditivityOut]
    additivity_ok: bool


def period_out(period) -> PeriodOut:  # noqa: ANN001 — periods.Period
    return PeriodOut(
        label=period.label, start=period.start, end=period.end, days=period.days
    )


def slice_out(period, amount: Money) -> SliceOut:  # noqa: ANN001 — periods.Period
    return SliceOut(
        label=period.label,
        start=period.start,
        end=period.end,
        amount=MoneyOut.of(amount),
    )


def merchant_out(line) -> MerchantOut:  # noqa: ANN001 — reports.MerchantLine
    from app.classify import categories as _cats  # noqa: PLC0415

    return MerchantOut(
        name=line.name,
        category=line.category_code,
        category_title=_cats.title(line.category_code),
        amount=MoneyOut.of(line.amount),
        count=line.count,
        average=MoneyOut.of(line.average),
        share=round(line.share, 4),
    )


def weekday_out(line) -> WeekdayOut:  # noqa: ANN001 — reports.WeekdayLine
    return WeekdayOut(
        index=line.index,
        title=line.title,
        short=line.short,
        amount=MoneyOut.of(line.amount),
        count=line.count,
        days=line.days,
        avg_per_day=MoneyOut.of(line.avg_per_day),
        share=round(line.share, 4),
    )


def average_check_out(line) -> AverageCheckOut:  # noqa: ANN001 — reports.AverageCheck
    return AverageCheckOut(
        code=line.code,
        title=line.title,
        emoji=line.emoji,
        amount=MoneyOut.of(line.amount),
        count=line.count,
        average=MoneyOut.of(line.average),
        biggest=MoneyOut.of(line.biggest),
    )


def subscription_out(line) -> SubscriptionOut:  # noqa: ANN001 — reports.SubscriptionLine
    from app.classify import categories as _cats  # noqa: PLC0415

    return SubscriptionOut(
        label=line.label,
        category=line.category_code,
        category_title=_cats.title(line.category_code),
        amount=MoneyOut.of(line.amount),
        period_days=line.period_days,
        occurrences=line.occurrences,
        monthly=MoneyOut.of(line.monthly),
        yearly=MoneyOut.of(line.yearly),
        last_charge_at=line.last_charge_at,
        next_charge_at=line.next_charge_at,
        paid_in_period=MoneyOut.of(line.paid_in_period),
        confirmed=line.confirmed,
    )


def category_change_out(line) -> CategoryChangeOut:  # noqa: ANN001 — reports.CategoryDelta
    return CategoryChangeOut(
        code=line.code,
        title=line.title,
        emoji=line.emoji,
        essential=line.essential,
        amount=MoneyOut.of(line.amount),
        prev_amount=MoneyOut.of(line.prev_amount),
        delta=MoneyOut.of(line.delta, sign=True),
        delta_pct=(round(line.delta_pct, 1) if line.delta_pct is not None else None),
        direction=line.direction,
    )


def additivity_out(check) -> AdditivityOut:  # noqa: ANN001 — reports.AdditivityCheck
    return AdditivityOut(
        parts=check.parts_name,
        title=check.title,
        total=MoneyOut.of(check.total),
        parts_total=MoneyOut.of(check.parts),
        parts_count=check.parts_count,
        ok=check.ok,
    )
