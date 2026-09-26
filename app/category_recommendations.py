"""Четыре наиболее полезных варианта для вопроса о категории расхода."""

from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.classify import categories as cats
from app.classify.enrich import category_from_text
from app.models import Decision, MerchantRule, Operation, ReviewItem

CATEGORY_QUESTIONS = frozenset({
    "unknown_place", "category_unclear", "large_uncategorized", "cash_spent_on",
})
FALLBACK = ("groceries", "cafe", "delivery", "taxi")


def category_options(kind: str) -> list[dict[str, str]]:
    """В полном списке доступны все применимые категории расходов."""
    if kind not in CATEGORY_QUESTIONS:
        return []
    codes = (code for code in cats.EXPENSE_CODES if code != cats.CASH_UNALLOCATED)
    if kind == "unknown_place":
        codes = (code for code in codes if code != cats.OTHER)
    return [{"code": code, "label": cats.title(code)} for code in codes]


def recommended_codes(session: Session, item: ReviewItem, options: list[dict]) -> list[str]:
    """Контекст операции, затем частота ответов человека, затем общий запасной порядок.

    В первую очередь учитываем его явные решения. Автоматические категории
    операций имеют меньший вес: они помогают при холодном старте, но не
    должны перебивать то, что человек уже выбирал сам.
    """
    allowed = {option["code"] for option in options}
    if not allowed:
        return []
    scores: Counter[str] = Counter()
    fallback_rank = {code: index for index, code in enumerate(FALLBACK)}

    decisions = session.scalars(
        select(Decision).where(Decision.user_id == item.user_id)
    ).all()
    counted: set[tuple[str, str]] = set()
    for decision in decisions:
        code = (decision.payload or {}).get("category")
        key = (decision.anchor_hash, code)
        if code in allowed and key not in counted:
            scores[code] += 8
            counted.add(key)

    for rule in session.scalars(
        select(MerchantRule).where(MerchantRule.user_id == item.user_id)
    ).all():
        if rule.title and rule.category_code in allowed:
            scores[rule.category_code] += 8

    for code, count in session.execute(
        select(Operation.category_code, func.count())
        .where(Operation.user_id == item.user_id, Operation.id != item.operation_id)
        .group_by(Operation.category_code)
    ).all():
        if code in allowed:
            scores[code] += min(count, 5)

    counterparty_id = item.operation.counterparty_id if item.operation else None
    if counterparty_id:
        for code, count in session.execute(
            select(Operation.category_code, func.count())
            .where(Operation.user_id == item.user_id,
                   Operation.counterparty_id == counterparty_id,
                   Operation.id != item.operation_id)
            .group_by(Operation.category_code)
        ).all():
            if code in allowed:
                scores[code] += 12 * count

    # Явная подсказка из описания операции сильнее общей популярности.
    context = item.operation.title if item.operation else ""
    hinted = category_from_text(context)
    if hinted in allowed:
        scores[hinted] += 100

    ranked = sorted(
        allowed,
        key=lambda code: (-scores[code], fallback_rank.get(code, len(FALLBACK)),
                          next(i for i, option in enumerate(options) if option["code"] == code)),
    )
    return ranked[:4]
