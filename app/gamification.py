"""Геймификация: уровень, достижения, задания и карта месяца.

Здесь нет ни одного придуманного числа. Это принципиальная позиция, а не
аккуратность: продукт называется «Честный месяц», и фальшивый прогресс в
нём обнуляет доверие ровно так же, как перевод себе, посчитанный расходом.

Отсюда три правила, которым подчинён весь модуль.

**XP не хранится.** Опыт — чистая функция от долговечных фактов: таблицы
`daily_activity`, рекорда серии и набранных достижений. Поэтому его нельзя
«потерять» при пересчёте, нельзя начислить дважды и не нужно чинить
миграцией. Тот же приём, что и во всей аналитике проекта: состояние
выводится из данных, а не накапливается отдельно.

**Достижения вычисляются, а не помечаются флагом.** У каждого своя функция
с докстрингом, где написано, из какого запроса берётся условие. Если данных
не хватает — достижение просто не выдано, и видно, чего именно не хватает.

**Что durable, а что нет.** `reclassify()` сносит и пересобирает Operation,
Posting, Claim, Subscription и ВСЕ ReviewItem. Значит «сколько вопросов
человек закрыл» из ReviewItem считать нельзя — этих строк уже нет. Счётчики
действий берутся только из `DailyActivity` и `Decision` (они переживают
пересчёт), а из проводок берётся текущее состояние: суммы, осознанность,
долги. Перепутать эти два источника — самая дорогая ошибка в модуле.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics import reports
from app.analytics.periods import Period, split_into_weeks
from app.models import (
    Account,
    AccountKind,
    Claim,
    ClaimStatus,
    Counterparty,
    DailyActivity,
    Decision,
    ImportBatch,
    Money,
    Operation,
    OperationKind,
    OperationSource,
    Posting,
    RawTransaction,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    Subscription,
    User,
    fmt_money,
)

# --------------------------------------------------------------------------
# Опыт и уровни
# --------------------------------------------------------------------------

# Сколько опыта стоит одно действие. Веса подобраны так, чтобы отражать
# ценность действия для качества данных, а не его трудоёмкость: загрузка
# выписки закрывает месяц операций разом и стоит дороже всего, ответ на
# спорный вопрос поднимает осознанность и стоит дороже ручного ввода.
XP_PER_TAP = 10  # ответ на спорную операцию
XP_PER_MANUAL = 8  # трата наличными, занесённая руками или голосом
XP_PER_IMPORT = 25  # загрузка выписки или скриншота
XP_PER_ACTIVE_DAY = 12  # сам факт, что в этот день человек что-то сделал
XP_PER_BEST_STREAK_DAY = 15  # рекорд серии: награждаем регулярность, а не разовый всплеск

# Титулы уровней. Выше последнего титул не меняется — врать про «легенду»
# на двадцатом уровне не нужно, лестница и так уходит в бесконечность.
LEVEL_TITLES: tuple[str, ...] = (
    "Наблюдатель",
    "Счетовод",
    "Внимательный к деталям",
    "Хозяин бюджета",
    "Знаток своих денег",
    "Мастер расклада",
    "Финансовый штурман",
    "Ревизор",
    "Архитектор бюджета",
    "Легенда честного месяца",
)


def xp_for_level(number: int) -> int:
    """Сколько опыта нужно, чтобы достичь уровня `number`.

    Квадратичная лестница 50·n·(n+1): 100 / 300 / 600 / 1000 / 1500 / 2100.
    Каждый следующий уровень дороже предыдущего ровно на 100 XP — прогресс
    не упирается в стену и не проскакивается за один вечер.
    """
    n = max(0, int(number))
    return 50 * n * (n + 1)


def level_for(xp: int) -> int:
    """Уровень по опыту — обратная к `xp_for_level`.

    Аналитическое решение уравнения 50·n·(n+1) ≤ xp плюс подгонка циклом:
    корень считается во float, и на границе ровно 1000 XP округление вниз
    отняло бы у человека честно набранный уровень.
    """
    xp = max(0, int(xp))
    n = max(0, int((math.sqrt(1 + xp / 12.5) - 1) / 2))
    while xp_for_level(n + 1) <= xp:
        n += 1
    while n > 0 and xp_for_level(n) > xp:
        n -= 1
    return n


@dataclass(slots=True)
class Level:
    number: int
    title: str
    xp: int
    xp_total_next: int

    @property
    def progress(self) -> float:
        """Доля пути до следующего уровня, 0..1."""
        floor = xp_for_level(self.number)
        span = self.xp_total_next - floor
        if span <= 0:
            return 1.0
        return max(0.0, min(1.0, (self.xp - floor) / span))


# --------------------------------------------------------------------------
# Долговечные факты: один проход по базе на весь расчёт
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Facts:
    """Снимок того, что о человеке достоверно известно.

    Собирается один раз и передаётся во все функции достижений: так каждая
    из них остаётся чистой (её легко проверить тестом, подсунув числа) и
    база не читается по десять раз на один ответ.
    """

    period: Period
    now: datetime

    # --- durable: переживает пересчёт ---
    taps: int = 0
    manual_entries: int = 0
    imports: int = 0
    active_days: int = 0
    streak_days: int = 0
    best_streak: int = 0
    decisions_total: int = 0
    decisions_in_period: int = 0
    imports_in_period: int = 0
    import_batches: int = 0
    raw_rows: int = 0
    manual_channel_rows: int = 0

    # --- состояние сейчас: пересобирается, но детерминированно ---
    banks_with_postings: int = 0
    months_covered: int = 0
    refunds: int = 0
    claims_total: int = 0
    claims_open: int = 0
    open_questions: int = 0
    subscriptions_active: int = 0
    awareness_percent: int = 0
    spend_current: Money = 0
    spend_previous: Money = 0
    period_closed: bool = False

    # --- даты, которые можно назвать честно ---
    first_import_at: date | None = None
    first_manual_at: date | None = None
    last_refund_at: date | None = None
    last_claim_closed_at: date | None = None
    streak_last_date: date | None = None


def activity_totals(session: Session, user_id: int) -> tuple[int, int, int, int]:
    """Сумма действий за всё время: тапы, ручной ввод, загрузки, активные дни.

    Единственный durable-источник «что человек делал»: ReviewItem после
    ответа физически удаляется пересчётом, а эта строка остаётся.
    """
    row = session.execute(
        select(
            func.coalesce(func.sum(DailyActivity.taps), 0),
            func.coalesce(func.sum(DailyActivity.manual_entries), 0),
            func.coalesce(func.sum(DailyActivity.imports), 0),
            func.count(func.distinct(DailyActivity.day)),
        ).where(DailyActivity.user_id == user_id)
    ).one()
    return int(row[0]), int(row[1]), int(row[2]), int(row[3])


def activity_by_day(session: Session, user_id: int, period: Period) -> dict[date, int]:
    """Сколько действий пришлось на каждый день периода.

    Нужно карте месяца и «тепловой» полоске: станция считается прожитой,
    если в её днях было хоть одно действие.
    """
    rows = session.execute(
        select(
            DailyActivity.day,
            DailyActivity.taps + DailyActivity.manual_entries + DailyActivity.imports,
        ).where(
            DailyActivity.user_id == user_id,
            DailyActivity.day >= period.start.date(),
            DailyActivity.day < period.end.date(),
        )
    ).all()
    return {day: int(total) for day, total in rows}


def decisions_count(session: Session, user_id: int, period: Period | None = None) -> int:
    """Сколько решений человек принял сам. Переживает пересчёт, в отличие от вопросов."""
    query = select(func.count()).select_from(Decision).where(Decision.user_id == user_id)
    if period is not None:
        query = query.where(
            Decision.created_at >= period.start, Decision.created_at < period.end
        )
    return int(session.scalar(query) or 0)


def distinct_banks(session: Session, user_id: int) -> int:
    """Сколько разных банков реально участвует в книге — считаем по счетам с проводками."""
    return int(
        session.scalar(
            select(func.count(func.distinct(Account.bank)))
            .select_from(Account)
            .join(Posting, Posting.account_id == Account.id)
            .where(Account.user_id == user_id, Account.bank.is_not(None))
        )
        or 0
    )


def months_covered(session: Session, user_id: int) -> int:
    """За сколько разных месяцев в книге есть расходные проводки."""
    return int(
        session.scalar(
            select(func.count(func.distinct(func.strftime("%Y-%m", Posting.at)))).where(
                Posting.user_id == user_id,
                Posting.account_kind == AccountKind.EXPENSE.value,
            )
        )
        or 0
    )


def manual_channel_count(session: Session, user_id: int) -> int:
    """Сколько фактов приехало голосом или текстом, а не файлом выписки."""
    return int(
        session.scalar(
            select(func.count())
            .select_from(RawTransaction)
            .where(
                RawTransaction.user_id == user_id,
                RawTransaction.source.in_(
                    [OperationSource.VOICE.value, OperationSource.MANUAL.value]
                ),
            )
        )
        or 0
    )


def refunds_count(session: Session, user_id: int) -> int:
    """Сколько возвратов товара распознано: возврат — минус к трате, а не доход."""
    return int(
        session.scalar(
            select(func.count())
            .select_from(Operation)
            .where(Operation.user_id == user_id, Operation.kind == OperationKind.REFUND)
        )
        or 0
    )


def claims_counts(session: Session, user_id: int) -> tuple[int, int]:
    """(всего обязательств, из них открытых). Долги и доли по общим счетам."""
    total = int(
        session.scalar(
            select(func.count()).select_from(Claim).where(Claim.user_id == user_id)
        )
        or 0
    )
    still_open = int(
        session.scalar(
            select(func.count())
            .select_from(Claim)
            .where(Claim.user_id == user_id, Claim.status == ClaimStatus.OPEN)
        )
        or 0
    )
    return total, still_open


def subscriptions_active_count(session: Session, user_id: int) -> int:
    """Сколько регулярных списаний найдено.

    Считаем только `active`: поле `confirmed` стирается пересчётом, и
    достижение по нему то появлялось бы, то исчезало.
    """
    return int(
        session.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.user_id == user_id, Subscription.active.is_(True))
        )
        or 0
    )


def collect_facts(
    session: Session, user: User, period: Period, *, now: datetime | None = None
) -> Facts:
    """Один проход по базе: всё, из чего потом считаются уровень и достижения."""
    now = now or datetime.now()
    taps, manual, imports, active_days = activity_totals(session, user.id)
    claims_total, claims_open = claims_counts(session, user.id)

    return Facts(
        period=period,
        now=now,
        taps=taps,
        manual_entries=manual,
        imports=imports,
        active_days=active_days,
        streak_days=user.streak_days,
        best_streak=user.best_streak,
        streak_last_date=user.streak_last_date,
        decisions_total=decisions_count(session, user.id),
        decisions_in_period=decisions_count(session, user.id, period),
        imports_in_period=int(
            session.scalar(
                select(func.coalesce(func.sum(DailyActivity.imports), 0)).where(
                    DailyActivity.user_id == user.id,
                    DailyActivity.day >= period.start.date(),
                    DailyActivity.day < period.end.date(),
                )
            )
            or 0
        ),
        import_batches=int(
            session.scalar(
                select(func.count())
                .select_from(ImportBatch)
                .where(ImportBatch.user_id == user.id)
            )
            or 0
        ),
        raw_rows=int(
            session.scalar(
                select(func.count())
                .select_from(RawTransaction)
                .where(RawTransaction.user_id == user.id)
            )
            or 0
        ),
        manual_channel_rows=manual_channel_count(session, user.id),
        banks_with_postings=distinct_banks(session, user.id),
        months_covered=months_covered(session, user.id),
        refunds=refunds_count(session, user.id),
        claims_total=claims_total,
        claims_open=claims_open,
        open_questions=reports.open_questions_count(session, user.id),
        subscriptions_active=subscriptions_active_count(session, user.id),
        awareness_percent=reports.awareness(session, user.id, period).percent,
        spend_current=reports.spend_total(session, user.id, period),
        spend_previous=reports.spend_total(session, user.id, period.shifted_back()),
        period_closed=period.end <= now,
        first_import_at=_scalar_date(
            session,
            select(func.min(ImportBatch.created_at)).where(ImportBatch.user_id == user.id),
        ),
        first_manual_at=_scalar_date(
            session,
            select(func.min(RawTransaction.booked_at)).where(
                RawTransaction.user_id == user.id,
                RawTransaction.source.in_(
                    [OperationSource.VOICE.value, OperationSource.MANUAL.value]
                ),
            ),
        ),
        last_refund_at=_scalar_date(
            session,
            select(func.max(Operation.occurred_at)).where(
                Operation.user_id == user.id, Operation.kind == OperationKind.REFUND
            ),
        ),
        last_claim_closed_at=_scalar_date(
            session,
            select(func.max(Claim.closed_at)).where(
                Claim.user_id == user.id, Claim.closed_at.is_not(None)
            ),
        ),
    )


def _scalar_date(session: Session, query) -> date | None:  # noqa: ANN001 — select(...)
    value = session.scalar(query)
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


# --------------------------------------------------------------------------
# Достижения
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Achievement:
    """Одно достижение в том виде, в каком его видит человек.

    `progress` для полученного всегда 1.0 — так требует контракт API.
    Ступень, до которой ещё идти, живёт в тексте `detail`, а не в прогрессе:
    иначе полученная ачивка выглядела бы недополученной.
    """

    code: str
    title: str
    detail: str
    earned: bool
    xp: int
    progress: float = 0.0
    hint: str | None = None
    earned_at: date | None = None


@dataclass(frozen=True, slots=True)
class Tier:
    """Ступень многоуровневого достижения: порог, награда и подпись."""

    need: int
    xp: int
    title: str
    detail: str


def _simple(
    code: str,
    *,
    title: str,
    detail: str,
    hint: str,
    xp: int,
    earned: bool,
    progress: float = 0.0,
    earned_at: date | None = None,
) -> Achievement:
    """Достижение без ступеней: либо есть, либо нет."""
    return Achievement(
        code=code,
        title=title,
        detail=detail if earned else hint,
        earned=earned,
        xp=xp if earned else 0,
        progress=1.0 if earned else max(0.0, min(1.0, progress)),
        hint=None if earned else hint,
        earned_at=earned_at if earned else None,
    )


def _tiered(
    code: str,
    value: int,
    tiers: tuple[Tier, ...],
    *,
    hint: str,
    earned_at: date | None = None,
) -> Achievement:
    """Достижение со ступенями: засчитывается высшая достигнутая.

    XP даёт только достигнутая ступень, а не сумма всех пройденных: иначе
    человек с рекордом в 30 дней получил бы опыт трижды за одну и ту же
    серию, и лестница уровней поехала бы.
    """
    reached = [t for t in tiers if value >= t.need]
    if not reached:
        first = tiers[0]
        return Achievement(
            code=code,
            title=first.title,
            detail=hint,
            earned=False,
            xp=0,
            progress=max(0.0, min(1.0, value / first.need)) if first.need else 0.0,
            hint=hint,
        )
    top = reached[-1]
    ahead = [t for t in tiers if value < t.need]
    detail = top.detail
    if ahead:
        detail = f"{top.detail}. Следующая ступень — {ahead[0].need}"
    return Achievement(
        code=code,
        title=top.title,
        detail=detail,
        earned=True,
        xp=top.xp,
        progress=1.0,
        hint=None,
        earned_at=earned_at,
    )


def ach_first_import(facts: Facts) -> Achievement:
    """«Первый шаг» — загружена хотя бы одна выписка.

    Условие: count(ImportBatch) ≥ 1. Дата получения — дата первой загрузки,
    она честно лежит в ImportBatch.created_at.
    """
    return _simple(
        "first_import",
        title="Первый шаг",
        detail="Загрузили выписку и увидели настоящие цифры",
        hint="Пришлите боту выписку из банка — она закроет месяц за одно действие",
        xp=50,
        earned=facts.import_batches >= 1,
        earned_at=facts.first_import_at,
    )


def ach_three_banks(facts: Facts) -> Achievement:
    """«Все карты на стол» — в книге счета трёх разных банков.

    Условие: count(distinct Account.bank) ≥ 3 среди счетов, по которым есть
    хотя бы одна проводка. Пустой заведённый счёт не считается: он ничего
    не добавляет к картине.
    """
    return _simple(
        "three_banks",
        title="Все карты на стол",
        detail=f"Счета {facts.banks_with_postings} банков в одной книге",
        hint="Загрузите выписки из трёх разных банков — тогда картина будет полной",
        xp=80,
        earned=facts.banks_with_postings >= 3,
        progress=facts.banks_with_postings / 3,
    )


def ach_streak(facts: Facts) -> Achievement:
    """«Серия» — рекорд подряд идущих дней с действиями, ступени 7 / 14 / 30.

    Условие: User.best_streak, который ведёт service.record_action(). Берём
    именно рекорд, а не текущую серию: один пропущенный день не должен
    отбирать уже полученное достижение.
    """
    return _tiered(
        "streak",
        facts.best_streak,
        (
            Tier(7, 60, "Серия: неделя", "Семь дней подряд без пропусков"),
            Tier(14, 120, "Серия: две недели", "Четырнадцать дней подряд"),
            Tier(30, 250, "Серия: месяц", "Тридцать дней подряд — это уже привычка"),
        ),
        hint="Заходите семь дней подряд — серия считается по факту действий",
        earned_at=facts.streak_last_date,
    )


def ach_awareness(facts: Facts) -> Achievement:
    """«Честные проценты» — осознанность за период, ступени 80 / 90 / 99 %.

    Условие: reports.awareness(...).percent. Это метрика качества данных:
    доля оборота, смысл которого системе понятен. Сто процентов недостижимы,
    пока висит хоть один неразобранный рубль, — поэтому верхняя ступень 99.

    Пустая книга даёт честные 100 % (неясного действительно нет), но хвалить
    там не за что: без трат достижение не выдаётся вовсе.
    """
    return _tiered(
        "awareness",
        facts.awareness_percent if facts.spend_current > 0 else 0,
        (
            Tier(80, 70, "Понятные деньги", "80 % оборота с установленным смыслом"),
            Tier(90, 140, "Почти всё ясно", "90 % оборота разобрано"),
            Tier(99, 250, "Честные 99 %", "Неясного почти не осталось"),
        ),
        hint="Ответьте на спорные вопросы — каждый ответ поднимает процент",
    )


def ach_clean_board(facts: Facts) -> Achievement:
    """«Чистая доска» — ни одного открытого вопроса при непустой книге.

    Условие: count(ReviewItem where status=OPEN) == 0 и count(RawTransaction) > 0.
    Второе обязательно: на пустой базе вопросов тоже ноль, но хвалить не за что.
    Прогресс идёт от числа открытых вопросов — чем их меньше, тем ближе.
    """
    earned = facts.open_questions == 0 and facts.raw_rows > 0
    left = facts.open_questions
    return _simple(
        "clean_board",
        title="Чистая доска",
        detail="Ни одного неразобранного вопроса",
        hint=(
            f"Осталось ответить на {left} — и доска чистая"
            if left
            else "Загрузите данные, а потом разберите спорные операции"
        ),
        xp=150,
        earned=earned,
        progress=1 / (1 + left) if facts.raw_rows else 0.0,
    )


def ach_voice(facts: Facts) -> Achievement:
    """«Голос за кадром» — наличные, занесённые голосом или текстом, 1 / 5 / 20.

    Условие: count(RawTransaction where source in ('voice','manual')). Это
    единственный канал, где человек вводит данные руками, — и единственное,
    чего нет ни в одной выписке.
    """
    return _tiered(
        "voice",
        facts.manual_channel_rows,
        (
            Tier(1, 40, "Голос за кадром", "Первая трата наличными, записанная голосом"),
            Tier(5, 100, "Наличные под контролем", "Пять трат наличными занесены"),
            Tier(20, 200, "Ничего не теряется", "Двадцать трат наличными занесены"),
        ),
        hint="Скажите боту «кофе 300» — наличные тоже попадут в отчёт",
        earned_at=facts.first_manual_at,
    )


def ach_three_months(facts: Facts) -> Achievement:
    """«Три месяца в книге» — расходные проводки минимум за три разных месяца.

    Условие: count(distinct strftime('%Y-%m', Posting.at)) ≥ 3 по расходным
    счетам. Считаем по проводкам, а не по датам выписок: месяц, из которого
    в книге ничего не осело, ничего и не показывает.
    """
    return _simple(
        "three_months",
        title="Три месяца в книге",
        detail=f"История за {facts.months_covered} месяца — есть с чем сравнивать",
        hint="Загрузите выписки за три месяца — появится сравнение периодов",
        xp=100,
        earned=facts.months_covered >= 3,
        progress=facts.months_covered / 3,
    )


def ach_debts_settled(facts: Facts) -> Achievement:
    """«Долги сведены» — все обязательства закрыты, и они вообще были.

    Условие: count(Claim where status='open') == 0 и count(Claim) ≥ 1. Долг
    и доля по общему счёту — один механизм, поэтому достижение закрывает и
    возвращённые займы, и возвращённые доли за ужин.
    """
    return _simple(
        "debts_settled",
        title="Долги сведены",
        detail="Ни одного открытого долга и ни одной невозвращённой доли",
        hint=(
            f"Открытых обязательств: {facts.claims_open}"
            if facts.claims_open
            else "Появится, когда будет что закрывать"
        ),
        xp=90,
        earned=facts.claims_total >= 1 and facts.claims_open == 0,
        progress=(
            (facts.claims_total - facts.claims_open) / facts.claims_total
            if facts.claims_total
            else 0.0
        ),
        earned_at=facts.last_claim_closed_at,
    )


def ach_refund(facts: Facts) -> Achievement:
    """«Вернул своё» — распознан хотя бы один возврат товара.

    Условие: count(Operation where kind='refund') ≥ 1. Ценность не в самом
    возврате, а в том, что он записан минусом к прошлой трате в её
    категории, а не доходом.
    """
    return _simple(
        "refund_found",
        title="Вернул своё",
        detail="Возврат записан минусом к трате, а не доходом",
        hint="Появится, когда в выписке встретится возврат товара",
        xp=60,
        earned=facts.refunds >= 1,
        earned_at=facts.last_refund_at,
    )


def ach_thrifty_month(facts: Facts) -> Achievement:
    """«Экономный месяц» — потрачено меньше, чем за предыдущий такой же срок.

    Условие: spend_total(период) < spend_total(период.shifted_back()) И
    период уже закончился. Второе принципиально: третьего числа траты
    заведомо меньше прошлого месяца, и без проверки достижение выдавалось бы
    авансом каждому — то есть было бы враньём.
    """
    earned = (
        facts.period_closed
        and facts.spend_previous > 0
        and facts.spend_current < facts.spend_previous
    )
    if facts.period_closed and facts.spend_previous > 0:
        saved = facts.spend_previous - facts.spend_current
        detail = f"Потратили на {fmt_money(abs(saved))} меньше, чем в прошлый раз"
        hint = f"В этот раз вышло на {fmt_money(abs(saved))} больше прошлого периода"
    else:
        detail = "Потратили меньше, чем в прошлый период"
        hint = "Считается по итогам завершённого месяца — аванса тут не бывает"
    return _simple(
        "thrifty_month",
        title="Экономный месяц",
        detail=detail,
        hint=hint,
        xp=120,
        earned=earned,
        progress=0.0,
    )


# Порядок важен: в этом же порядке достижения приезжают на экран и
# раскладываются сундуками по карте месяца.
ACHIEVEMENTS = (
    ach_first_import,
    ach_three_banks,
    ach_streak,
    ach_awareness,
    ach_clean_board,
    ach_voice,
    ach_three_months,
    ach_debts_settled,
    ach_refund,
    ach_thrifty_month,
)


def achievements(facts: Facts) -> list[Achievement]:
    """Все достижения в фиксированном порядке — и полученные, и нет."""
    return [rule(facts) for rule in ACHIEVEMENTS]


def total_xp(facts: Facts, earned: list[Achievement] | None = None) -> int:
    """Опыт целиком.

        XP = 10·тапы + 8·ручной ввод + 25·загрузки + 12·активных дней
           + 15·рекорд серии
           + сумма XP полученных достижений

    Ничего не хранится: та же функция от тех же фактов всегда даёт тот же
    ответ, поэтому опыт нельзя потерять пересчётом и нельзя начислить дважды.
    """
    items = achievements(facts) if earned is None else earned
    base = (
        XP_PER_TAP * facts.taps
        + XP_PER_MANUAL * facts.manual_entries
        + XP_PER_IMPORT * facts.imports
        + XP_PER_ACTIVE_DAY * facts.active_days
    )
    streak = XP_PER_BEST_STREAK_DAY * facts.best_streak
    return base + streak + sum(a.xp for a in items if a.earned)


def level_of(xp: int) -> Level:
    """Уровень, титул и порог следующего уровня по набранному опыту."""
    number = level_for(xp)
    return Level(
        number=number,
        title=LEVEL_TITLES[min(number, len(LEVEL_TITLES) - 1)],
        xp=xp,
        xp_total_next=xp_for_level(number + 1),
    )


# --------------------------------------------------------------------------
# Задания
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Quest:
    """Задание в том виде, в каком его видит человек.

    `kind` — «system» (наше) или «sponsor» (партнёрское). Поля `sponsor` и
    `reward` заполнены только у партнёрских.
    """

    code: str
    kind: str
    title: str
    detail: str
    progress: float
    xp: int
    done: bool
    sponsor: str | None = None
    reward: str | None = None
    deadline: date | None = None


@dataclass(frozen=True, slots=True)
class PartnerQuest:
    """Партнёрское задание. Статика в коде — витрина, но не прогресс.

    Интеграции со спонсором не нужно: покупка и так приедет в выписке.
    Условие — предикат над проводками: траты у конкретного продавца
    (`match_key` — Counterparty.norm_key) или в конкретной категории
    (`match_category`). Выполнение считается запросом к настоящим данным,
    поэтому нарисовать прогресс здесь физически нечем.
    """

    code: str
    sponsor: str
    title: str
    goal: str
    reward: str
    xp: int
    ends_day: int  # день месяца, до которого действует предложение
    match_key: str | None = None
    match_category: str | None = None
    min_amount: Money = 0  # копейки
    min_count: int = 0

    def __post_init__(self) -> None:
        if not (self.match_key or self.match_category):
            raise ValueError(f"{self.code}: задание без условия совпадёт с любой тратой")
        if self.min_amount <= 0 and self.min_count <= 0:
            raise ValueError(f"{self.code}: задание без порога выполнено всегда")


# Витрина партнёров. Названия и награды — демонстрационные, а вот ключи
# продавцов настоящие: ровно так их нормализует app.ledger.accounts.norm_key,
# когда разбирает выписку. Поэтому прогресс считается по реальным покупкам.
PARTNER_QUESTS: tuple[PartnerQuest, ...] = (
    PartnerQuest(
        code="surf_coffee_2000",
        sponsor="Surf Coffee",
        title="Кофе на 2 000 ₽",
        goal="потратьте 2 000 ₽ в Surf Coffee за месяц",
        reward="Капучино в подарок",
        xp=60,
        ends_day=24,
        match_key="surf coffee",
        min_amount=200_000,
    ),
    PartnerQuest(
        code="yandex_go_5",
        sponsor="Яндекс Go",
        title="Пять поездок",
        goal="совершите 5 поездок за месяц",
        reward="Промокод −30 % на следующую поездку",
        xp=50,
        ends_day=20,
        match_key="yandex go",
        min_count=5,
    ),
    PartnerQuest(
        code="lenta_10000",
        sponsor="Лента",
        title="Продукты на 10 000 ₽",
        goal="купите продуктов на 10 000 ₽",
        reward="500 бонусных баллов",
        xp=80,
        ends_day=27,
        match_key="lenta hyper",
        min_amount=1_000_000,
    ),
    PartnerQuest(
        code="dodo_700",
        sponsor="Додо Пицца",
        title="Первый заказ",
        goal="закажите на 700 ₽",
        reward="Додстер в подарок",
        xp=40,
        ends_day=31,
        match_key="dodo pizza",
        min_amount=70_000,
    ),
)


def quest_deadline(period: Period, day_of_month: int) -> date:
    """Последний день действия предложения, обрезанный границами периода."""
    last = (period.end - timedelta(days=1)).date()
    first = period.start.date()
    try:
        wanted = date(period.start.year, period.start.month, day_of_month)
    except ValueError:  # 31-е в тридцатидневном месяце
        wanted = last
    return min(max(wanted, first), last)


def quest_window(period: Period, deadline: date) -> Period:
    """Окно, в котором засчитываются траты: от начала периода до дедлайна включительно."""
    end = min(
        period.end,
        datetime(deadline.year, deadline.month, deadline.day) + timedelta(days=1),
    )
    return Period(period.start, max(period.start, end), f"до {deadline:%d.%m}")


def quest_progress(
    session: Session, user_id: int, quest: PartnerQuest, window: Period
) -> tuple[Money, int]:
    """(сумма трат, число операций) по условию задания за окно.

    Один запрос по проводкам расходных счетов с присоединённым контрагентом.
    Считаем по Posting, а не по Operation.amount: только так сумма задания
    сходится с тем, что человек видит в отчёте за тот же период.
    """
    query = (
        select(
            func.coalesce(func.sum(Posting.amount), 0),
            func.count(func.distinct(Posting.operation_id)),
        )
        .join(Operation, Operation.id == Posting.operation_id)
        .outerjoin(Counterparty, Counterparty.id == Operation.counterparty_id)
        .where(
            Posting.user_id == user_id,
            Posting.account_kind == AccountKind.EXPENSE.value,
            Posting.at >= window.start,
            Posting.at < window.end,
        )
    )
    if quest.match_key:
        query = query.where(Counterparty.norm_key == quest.match_key)
    if quest.match_category:
        query = query.where(Posting.category_code == quest.match_category)

    amount, count = session.execute(query).one()
    # Возврат товара у того же продавца уменьшает сумму — это честно, но в
    # минус задание уходить не должно.
    return max(0, int(amount or 0)), int(count or 0)


def partner_quests(session: Session, user_id: int, period: Period) -> list[Quest]:
    """Партнёрские задания с прогрессом, посчитанным по настоящим тратам."""
    out: list[Quest] = []
    for offer in PARTNER_QUESTS:
        deadline = quest_deadline(period, offer.ends_day)
        amount, count = quest_progress(
            session, user_id, offer, quest_window(period, deadline)
        )

        ratios: list[float] = []
        parts: list[str] = []
        if offer.min_amount > 0:
            ratios.append(amount / offer.min_amount)
            parts.append(f"{fmt_money(amount)} из {fmt_money(offer.min_amount)}")
        if offer.min_count > 0:
            ratios.append(count / offer.min_count)
            parts.append(f"{count} из {offer.min_count}")
        progress = max(0.0, min(1.0, min(ratios) if ratios else 0.0))

        goal = offer.goal[:1].upper() + offer.goal[1:]
        out.append(
            Quest(
                code=offer.code,
                kind="sponsor",
                title=offer.title,
                detail=f"{goal}. Сейчас {', '.join(parts)}",
                progress=progress,
                xp=offer.xp,
                done=progress >= 1.0,
                sponsor=offer.sponsor,
                reward=offer.reward,
                deadline=deadline,
            )
        )
    return out


def unknown_incoming_debt(session: Session, user_id: int) -> tuple[int, Money]:
    """Сколько поступлений висит неразобранными и на какую сумму.

    Ровно тот вопрос, который двигает осознанность сильнее всего: деньги на
    карте видны, но доходом они не объявлены.
    """
    rows = session.execute(
        select(func.count(), func.coalesce(func.sum(Operation.amount), 0))
        .select_from(ReviewItem)
        .join(Operation, Operation.id == ReviewItem.operation_id)
        .where(
            ReviewItem.user_id == user_id,
            ReviewItem.status == ReviewStatus.OPEN,
            ReviewItem.kind == ReviewKind.INCOMING_UNKNOWN,
        )
    ).one()
    return int(rows[0] or 0), int(rows[1] or 0)


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def system_quests(session: Session, user_id: int, facts: Facts) -> list[Quest]:
    """Наши собственные задания. Каждое считается из durable-фактов.

    Ни одно не опирается на ReviewItem как на счётчик сделанного: отвеченный
    вопрос удаляется пересчётом. Там, где нужно «сколько человек ответил»,
    считается count(Decision) — единственный след его собственных решений.
    """
    period = facts.period
    deadline = (period.end - timedelta(days=1)).date()
    count, amount = unknown_incoming_debt(session, user_id)

    return [
        Quest(
            code="resolve_unknown",
            kind="system",
            title="Разобрать поступления",
            detail=(
                f"{count} {_plural(count, 'операция', 'операции', 'операций')} "
                f"на {fmt_money(amount)}"
                if count
                else "Неразобранных поступлений нет"
            ),
            progress=0.0 if count else 1.0,
            xp=10,
            done=count == 0,
            deadline=deadline,
        ),
        Quest(
            code="answer_three",
            kind="system",
            title="Три ответа за месяц",
            detail=f"{facts.decisions_in_period} из 3 спорных операций закрыто",
            progress=max(0.0, min(1.0, facts.decisions_in_period / 3)),
            xp=30,
            done=facts.decisions_in_period >= 3,
            deadline=deadline,
        ),
        Quest(
            code="import_month",
            kind="system",
            title="Закрыть месяц выпиской",
            detail=(
                "Выписка за этот месяц загружена"
                if facts.imports_in_period
                else "Одна загрузка закрывает месяц операций разом"
            ),
            progress=1.0 if facts.imports_in_period else 0.0,
            xp=25,
            done=facts.imports_in_period >= 1,
            deadline=deadline,
        ),
        Quest(
            code="awareness_95",
            kind="system",
            title="Осознанность 95 %",
            detail=f"Сейчас {facts.awareness_percent} % оборота с понятным смыслом",
            progress=max(0.0, min(1.0, facts.awareness_percent / 95)),
            xp=40,
            done=facts.awareness_percent >= 95,
            deadline=deadline,
        ),
        Quest(
            code="streak_week",
            kind="system",
            title="Неделя без пропусков",
            detail=(
                f"Серия {facts.streak_days} "
                f"{_plural(facts.streak_days, 'день', 'дня', 'дней')} подряд"
            ),
            progress=max(0.0, min(1.0, facts.streak_days / 7)),
            xp=35,
            done=facts.streak_days >= 7,
            deadline=deadline,
        ),
    ]


def quests(session: Session, user_id: int, facts: Facts) -> list[Quest]:
    """Все задания: сначала наши, потом партнёрские."""
    return system_quests(session, user_id, facts) + partner_quests(
        session, user_id, facts.period
    )


# --------------------------------------------------------------------------
# Карта месяца
# --------------------------------------------------------------------------

STATE_DONE = "done"
STATE_CURRENT = "current"
STATE_LOCKED = "locked"


@dataclass(slots=True)
class Station:
    """Станция на тропе месяца.

    Состояний три, и у каждого один смысл:

        done    — уже позади: неделя закончилась, сундук собран, задание выполнено;
        current — можно прямо сейчас: текущая неделя, открытый вопрос, активное задание;
        locked  — впереди или закрыто: будущая неделя, несобранный сундук, финиш.

    Грош стоит на станции с kind="week" и state="current" — ровно там, где
    месяц находится сегодня. Остальные «current» подсвечиваются как дела,
    до которых можно дотянуться не сходя с места.

    `amount` — деньги, привязанные к станции: недельная сумма, сумма спорной
    операции, прогноз на финише. Там, где денег нет (сундук, задание), None,
    а не ноль: ноль здесь означал бы «потрачено ноль».
    """

    code: str
    kind: str  # week | question | chest | quest | finish
    state: str
    title: str
    detail: str
    amount: Money | None = None
    action: dict | None = None


def _week_of(moment: datetime, weeks: list[Period]) -> int:
    """Номер недели, в которую попадает момент. -1 — момент раньше периода."""
    for index, chunk in enumerate(weeks):
        if chunk.contains(moment):
            return index
    return -1 if not weeks or moment < weeks[0].start else len(weeks)


def _chest_plan(
    items: list[Achievement], weeks: int, current_week: int
) -> dict[int, Achievement]:
    """Куда на тропе положить сундуки.

    Позади Гроша лежат уже собранные достижения, впереди — те, до которых
    ещё идти. Так тропа сама рассказывает историю: что сделано — слева,
    что осталось — справа. Сундуки ставятся через неделю, чтобы тропа не
    превратилась в сплошной ряд ящиков.
    """
    earned = [a for a in items if a.earned]
    ahead = sorted(
        (a for a in items if not a.earned), key=lambda a: a.progress, reverse=True
    )
    plan: dict[int, Achievement] = {}
    for index in range(0, weeks, 2):
        pool = earned if index < current_week else ahead
        if pool:
            plan[index] = pool.pop(0)
    return plan


def month_map(
    session: Session,
    user_id: int,
    facts: Facts,
    *,
    items: list[Achievement],
    offers: list[Quest],
) -> list[Station]:
    """Тропа месяца: недели, вопросы, сундуки, партнёрские задания и финиш.

    Порядок станций — хронологический, и это не украшение: неделя идёт за
    неделей, спорная операция стоит в той неделе, когда она случилась,
    партнёрское задание — в неделе своего дедлайна. Поэтому станция, на
    которой стоит Грош (неделя со state="current"), всегда совпадает с тем
    местом месяца, где человек находится сегодня.

    Суммы недельных станций — это `reports.spend_total` по тем же
    полуинтервалам, что и в отчёте. Их сумма равна месячной по построению,
    и карта не может показать одно, а вкладка отчётов другое.
    """
    period = facts.period
    now = facts.now
    weeks = split_into_weeks(period)
    current_week = _week_of(now, weeks)
    if current_week < 0:
        current_week = 0

    questions = _open_questions_by_week(session, user_id, weeks, current_week)
    chests = _chest_plan(items, len(weeks), current_week)
    sponsor_by_week: dict[int, list[Quest]] = {}
    for offer in offers:
        if offer.kind != "sponsor" or offer.deadline is None:
            continue
        moment = datetime(offer.deadline.year, offer.deadline.month, offer.deadline.day)
        index = min(max(0, _week_of(moment, weeks)), max(0, len(weeks) - 1))
        sponsor_by_week.setdefault(index, []).append(offer)

    out: list[Station] = []
    for index, chunk in enumerate(weeks):
        if chunk.end <= now:
            week_state = STATE_DONE
        elif chunk.contains(now):
            week_state = STATE_CURRENT
        else:
            week_state = STATE_LOCKED
        out.append(
            Station(
                code=f"w{index + 1}",
                kind="week",
                state=week_state,
                title=f"Неделя {index + 1}",
                detail=chunk.label,
                amount=reports.spend_total(session, user_id, chunk),
                action={"type": "tab", "tab": "reports"},
            )
        )
        for item, amount in questions.get(index, []):
            # Открытый вопрос всегда можно закрыть одним тапом — независимо
            # от того, в какой неделе лежит его операция.
            out.append(
                Station(
                    code=f"q{item.id}",
                    kind="question",
                    state=STATE_CURRENT,
                    title="Спорная операция",
                    detail=item.question,
                    amount=amount,
                    action={"type": "question", "id": item.id},
                )
            )
        for offer in sponsor_by_week.get(index, []):
            out.append(
                Station(
                    code=f"quest-{offer.code}",
                    kind="quest",
                    state=(
                        STATE_DONE
                        if offer.done
                        else STATE_CURRENT
                        if offer.deadline is None or offer.deadline >= now.date()
                        else STATE_LOCKED
                    ),
                    title=f"{offer.sponsor}: {offer.title}",
                    detail=offer.detail,
                    action={"type": "tab", "tab": "rewards"},
                )
            )
        chest = chests.get(index)
        if chest is not None:
            # Несобранный сундук так и рисуется закрытым: locked здесь — это
            # не «недоступно», а «ещё не открыт».
            out.append(
                Station(
                    code=f"chest-{chest.code}",
                    kind="chest",
                    state=STATE_DONE if chest.earned else STATE_LOCKED,
                    title=chest.title,
                    detail=chest.detail,
                    action={"type": "tab", "tab": "rewards"},
                )
            )

    out.append(_finish(session, user_id, facts))
    return out


def _open_questions_by_week(
    session: Session, user_id: int, weeks: list[Period], current_week: int
) -> dict[int, list[tuple[ReviewItem, Money | None]]]:
    """Открытые вопросы, разложенные по неделям по дате своей операции.

    Вопрос без операции или с операцией вне периода встаёт в текущую неделю:
    спрашивают-то всё равно сейчас.
    """
    rows = session.scalars(
        select(ReviewItem)
        .where(ReviewItem.user_id == user_id, ReviewItem.status == ReviewStatus.OPEN)
        .order_by(ReviewItem.priority.desc(), ReviewItem.id)
    ).all()
    out: dict[int, list[tuple[ReviewItem, Money | None]]] = {}
    fallback = min(max(0, current_week), max(0, len(weeks) - 1))
    for item in rows:
        op = item.operation
        index = _week_of(op.occurred_at, weeks) if op is not None else -1
        if index < 0 or index >= len(weeks):
            index = fallback
        out.setdefault(index, []).append((item, op.amount if op is not None else None))
    return out


def _finish(session: Session, user_id: int, facts: Facts) -> Station:
    """Финиш месяца: прогноз, пока месяц идёт, и итог, когда он закрыт.

    Прогноз показывается только там, где он осмыслен: `Forecast.is_meaningful`
    гасит его в первые три дня периода и в уже закрытом периоде.
    """
    period = facts.period
    if facts.period_closed:
        return Station(
            code="finish",
            kind="finish",
            state=STATE_DONE,
            title="Месяц закрыт",
            detail=f"Итог: {fmt_money(facts.spend_current)}",
            amount=facts.spend_current,
            action={"type": "tab", "tab": "reports"},
        )

    prediction = reports.forecast(session, user_id, period, now=facts.now)
    if prediction is not None and prediction.is_meaningful:
        detail = f"По текущему темпу выйдет {fmt_money(prediction.projected)}"
        amount: Money | None = prediction.projected
    else:
        detail = "Прогноз появится, когда месяца пройдёт достаточно"
        amount = None
    return Station(
        code="finish",
        kind="finish",
        state=STATE_LOCKED,
        title="Финиш месяца",
        detail=detail,
        amount=amount,
        action={"type": "tab", "tab": "reports"},
    )


# --------------------------------------------------------------------------
# Сборка состояния
# --------------------------------------------------------------------------


@dataclass(slots=True)
class GameState:
    level: Level
    quests: list[Quest] = field(default_factory=list)
    achievements: list[Achievement] = field(default_factory=list)
    map: list[Station] = field(default_factory=list)


def game_state(
    session: Session, user: User, period: Period, *, now: datetime | None = None
) -> GameState:
    """Всё игровое состояние одним проходом: уровень, задания, ачивки, карта."""
    facts = collect_facts(session, user, period, now=now)
    items = achievements(facts)
    offers = quests(session, user.id, facts)
    return GameState(
        level=level_of(total_xp(facts, items)),
        quests=offers,
        achievements=items,
        map=month_map(session, user.id, facts, items=items, offers=offers),
    )
