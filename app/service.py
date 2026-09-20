"""Сценарии использования. Один слой на бота и на Mini App.

Бот и HTTP-API — это две оболочки вокруг одних и тех же функций. Логика
живёт здесь, чтобы «ответить на вопрос одним тапом» вело себя одинаково,
откуда бы тап ни пришёл.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics import reports
from app.analytics.periods import Period, month, week
from app.classify import categories as cats
from app.classify.enrich import category_from_text
from app.classify.pipeline import DecisionKind, reclassify
from app.ingest.base import ParsedRow
from app.ingest.service import ImportResult, add_manual, import_file, import_images
from app.models import (
    DailyActivity,
    Decision,
    Money,
    Operation,
    OperationSource,
    ReviewItem,
    ReviewStatus,
    User,
)


@dataclass(slots=True)
class IngestOutcome:
    imported: ImportResult | None
    operations: int
    questions: int
    unresolved: Money
    error: str = ""
    # Что именно распознали на картинке: statement, receipt или other.
    image_kind: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


# --- пользователь и активность --------------------------------------------


def get_or_create_user(session: Session, tg_id: int, display_name: str = "") -> User:
    user = session.scalar(select(User).where(User.tg_id == tg_id))
    if user is None:
        user = User(tg_id=tg_id, display_name=display_name)
        session.add(user)
        session.flush()
    elif display_name and not user.display_name:
        user.display_name = display_name
    return user


def record_action(
    session: Session, user: User, *, taps: int = 0, manual: int = 0, imports: int = 0
) -> None:
    """Считает действия пользователя и ведёт стрик.

    Число действий в день мы не объявляем, а измеряем: на защите называется
    посчитанное значение, а не желаемое.
    """
    today = date.today()
    row = session.scalar(
        select(DailyActivity).where(
            DailyActivity.user_id == user.id, DailyActivity.day == today
        )
    )
    if row is None:
        row = DailyActivity(user_id=user.id, day=today)
        session.add(row)
        session.flush()
    row.taps += taps
    row.manual_entries += manual
    row.imports += imports

    if user.streak_last_date == today:
        return
    if user.streak_last_date == today - timedelta(days=1):
        user.streak_days += 1
    else:
        user.streak_days = 1
    user.streak_last_date = today
    user.best_streak = max(user.best_streak, user.streak_days)


def average_actions_per_day(session: Session, user_id: int, days: int = 30) -> float:
    """Сколько действий в день инструмент реально требует.

    Знаменатель — дни, что человек им пользуется, а не фиксированное окно:
    если он с нами двенадцать дней, делить на тридцать значит занизить
    цифру втрое. Это то самое число, которое кейс просит назвать, и
    занижать его в свою пользу нельзя ровно так же, как завышать.
    """
    today = date.today()
    since = today - timedelta(days=days)
    rows = session.scalars(
        select(DailyActivity).where(
            DailyActivity.user_id == user_id, DailyActivity.day >= since
        )
    ).all()
    if not rows:
        return 0.0
    first = min(r.day for r in rows)
    span = max(1, (today - first).days + 1)
    total = sum(r.taps + r.manual_entries + r.imports for r in rows)
    return total / span


# --- загрузка данных -------------------------------------------------------


def ingest_statement(
    session: Session, user: User, data: bytes, filename: str
) -> IngestOutcome:
    try:
        result = import_file(session, user.id, data, filename=filename)
    except ValueError as exc:
        return IngestOutcome(None, 0, 0, 0, error=str(exc))
    record_action(session, user, imports=1)
    report = reclassify(session, user.id)
    return IngestOutcome(result, report.operations, report.questions, report.unresolved_amount)


def ingest_screenshot(session: Session, user: User, images: list[bytes]) -> IngestOutcome:
    """Картинка от пользователя: скриншот приложения банка или фото чека."""
    result, kind = import_images(session, user.id, images)
    if result.batch.rows_total == 0:
        return IngestOutcome(
            result, 0, 0, 0,
            error="не удалось прочитать операции на картинке",
            image_kind=kind,
        )
    record_action(session, user, imports=1)
    report = reclassify(session, user.id)
    return IngestOutcome(
        result, report.operations, report.questions, report.unresolved_amount,
        image_kind=kind,
    )


# --- быстрый ручной ввод ---------------------------------------------------

_AMOUNT_IN_TEXT = re.compile(r"(\d[\d\s.,]*)\s*(?:₽|р\.?|руб\w*)?", re.IGNORECASE)


def parse_quick_entry(text: str, *, when: datetime | None = None) -> ParsedRow | None:
    """Разбирает «кофе 300» или «такси 450» в трату наличными.

    Ручной ввод нужен ровно для того, чего нет в выписке: наличных. Всё
    остальное приходит само, и просить это вводить было бы ошибкой.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    match = _AMOUNT_IN_TEXT.search(raw)
    if match is None:
        return None
    digits = match.group(1).replace(" ", "").replace(" ", "")
    if "," in digits and "." in digits:
        digits = digits.replace(".", "").replace(",", ".")
    else:
        digits = digits.replace(",", ".")
    try:
        amount = float(digits)
    except ValueError:
        return None
    if amount <= 0:
        return None

    label = (raw[: match.start()] + " " + raw[match.end() :]).strip(" .,;:-") or "Трата наличными"
    label = re.sub(r"\s+", " ", label)
    return ParsedRow(
        booked_at=when or datetime.now(),
        amount=-int(round(amount * 100)),
        description=label,
        counterparty_raw=label,
        bank="Наличные",
        category_hint=category_from_text(label),
    )


def add_quick_expense(
    session: Session,
    user: User,
    text: str,
    *,
    source: OperationSource = OperationSource.MANUAL,
    when: datetime | None = None,
) -> tuple[ParsedRow | None, IngestOutcome]:
    row = parse_quick_entry(text, when=when)
    if row is None:
        return None, IngestOutcome(None, 0, 0, 0, error="не нашёл сумму в сообщении")
    result = add_manual(session, user.id, [row], origin=source)
    record_action(session, user, manual=1)
    report = reclassify(session, user.id)
    return row, IngestOutcome(
        result, report.operations, report.questions, report.unresolved_amount
    )


# --- очередь вопросов ------------------------------------------------------


def open_questions(session: Session, user_id: int, limit: int = 20) -> list[ReviewItem]:
    return list(
        session.scalars(
            select(ReviewItem)
            .where(ReviewItem.user_id == user_id, ReviewItem.status == ReviewStatus.OPEN)
            .order_by(ReviewItem.priority.desc(), ReviewItem.id)
            .limit(limit)
        ).all()
    )


def count_open_questions(session: Session, user_id: int) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(ReviewItem)
            .where(ReviewItem.user_id == user_id, ReviewItem.status == ReviewStatus.OPEN)
        )
        or 0
    )


# Какой вид решения записывается при ответе на вопрос каждого вида.
_ANSWER_TO_DECISION = {
    "incoming_unknown": DecisionKind.SET_KIND,
    "outgoing_to_person": DecisionKind.SET_KIND,
    "shared_bill_heads": DecisionKind.PARTICIPANTS,
    "large_uncategorized": DecisionKind.SET_CATEGORY,
    "category_unclear": DecisionKind.SET_CATEGORY,
    "is_self_transfer": DecisionKind.FORCE_TRANSFER,
    "cash_spent_on": DecisionKind.CASH_ALLOCATION,
}


def answer_question(
    session: Session, user: User, review_id: int, code: str
) -> ReviewItem | None:
    """Один тап пользователя: записывает решение и пересчитывает всё заново.

    Решение живёт отдельно от операций и переживает пересчёт — поэтому один
    и тот же вопрос никогда не задаётся дважды.
    """
    item = session.get(ReviewItem, review_id)
    if item is None or item.user_id != user.id or item.status != ReviewStatus.OPEN:
        return None

    kind = _ANSWER_TO_DECISION.get(str(item.kind), DecisionKind.SET_KIND)
    payload: dict = {}
    if kind == DecisionKind.PARTICIPANTS:
        if not code.isdigit():
            return None
        payload = {"n": int(code)}
    elif kind in (DecisionKind.SET_CATEGORY, DecisionKind.CASH_ALLOCATION):
        payload = {"category": code}
    elif kind == DecisionKind.FORCE_TRANSFER:
        if code == "no":
            kind = DecisionKind.NOT_TRANSFER
        payload = {}
    else:
        payload = {"kind": code}

    existing = session.scalar(
        select(Decision).where(
            Decision.user_id == user.id,
            Decision.anchor_hash == item.anchor_hash,
            Decision.kind == kind,
        )
    )
    if existing is None:
        session.add(
            Decision(
                user_id=user.id, anchor_hash=item.anchor_hash, kind=kind, payload=payload
            )
        )
    else:
        existing.payload = payload

    item.status = ReviewStatus.RESOLVED
    item.answer_code = code
    item.resolved_at = datetime.now()
    record_action(session, user, taps=1)
    session.flush()

    reclassify(session, user.id)
    return item


def skip_question(session: Session, user: User, review_id: int) -> None:
    item = session.get(ReviewItem, review_id)
    if item is not None and item.user_id == user.id:
        item.status = ReviewStatus.SKIPPED
        item.resolved_at = datetime.now()


# --- отчёты ----------------------------------------------------------------


def report_for(session: Session, user_id: int, period: Period) -> reports.PeriodReport:
    return reports.build_report(session, user_id, period)


def current_month(today: date | None = None) -> Period:
    today = today or date.today()
    return month(today.year, today.month)


def current_week(today: date | None = None) -> Period:
    return week(today or date.today())


def latest_data_month(session: Session, user_id: int) -> Period:
    """Месяц, за который вообще есть данные.

    На демонстрации это важно: набор описывает сентябрь, а календарь может
    показывать что угодно. Отчёт должен открываться там, где есть цифры.
    """
    last = session.scalar(
        select(func.max(Operation.occurred_at)).where(Operation.user_id == user_id)
    )
    if last is None:
        return current_month()
    return month(last.year, last.month)


def top_categories(report: reports.PeriodReport, limit: int = 6) -> list[reports.CategoryLine]:
    return report.categories[:limit]


def category_label(code: str | None) -> str:
    return cats.label(code)
