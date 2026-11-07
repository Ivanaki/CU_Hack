"""Категоризация незнакомых продавцов языковой моделью.

Кейс называет это направлением для проработки: «разбор назначений платежей
и названий магазинов языковой моделью». Здесь оно сделано так, чтобы
оставаться дешёвым и необязательным:

  * спрашиваем только про то, что не разобрали MCC и словарь;
  * спрашиваем пачкой — один запрос на все незнакомые названия сразу;
  * ответ сохраняем как правило в базе, поэтому за один и тот же магазин
    платим ровно один раз;
  * модель выбирает только из нашего списка категорий, придумать своё
    она не может;
  * при любом сбое возвращаем пусто, и всё работает на эвристиках.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classify import categories as cats
from app.ledger.accounts import norm_key
from app.llm.client import LLM, get_llm
from app.models import MatchType, MerchantRule

log = logging.getLogger(__name__)

# Больше за раз спрашивать незачем: за месяц новых продавцов обычно единицы,
# а длинный список ухудшает качество ответа.
MAX_PER_CALL = 40

SYSTEM = (
    "Ты относишь названия продавцов из банковских выписок к категориям трат. "
    "Отвечаешь только JSON. Категорию выбираешь строго из предложенного списка. "
    "Если по названию непонятно, ставишь other — это нормальный ответ, "
    "угадывать не нужно."
)


def _prompt(names: list[str]) -> str:
    allowed = ", ".join(
        f"{c.code} ({c.title})" for c in cats.CATEGORIES if not c.is_income
    )
    listed = "\n".join(f"- {n}" for n in names)
    return (
        f"Допустимые категории: {allowed}\n\n"
        f"Названия продавцов из российских банковских выписок:\n{listed}\n\n"
        'Верни JSON: {"items": [{"name": "<название как дано>", '
        '"category": "<код категории>"}]}'
    )


def known_rules(session: Session, user_id: int) -> dict[str, str]:
    """Уже известные соответствия: и глобальные, и выученные для пользователя."""
    rules = session.scalars(
        select(MerchantRule).where(
            MerchantRule.match_type == MatchType.EXACT,
            MerchantRule.user_id.in_([user_id, None]),
        )
    ).all()
    return {rule.pattern: rule.category_code for rule in rules}


def classify_merchants(
    session: Session,
    user_id: int,
    names: list[str],
    *,
    llm: LLM | None = None,
) -> dict[str, str]:
    """Названия -> коды категорий. Пустой результат означает «не вышло»."""
    llm = llm or get_llm()
    if not llm.available or not names:
        return {}

    cached = known_rules(session, user_id)
    unknown = [n for n in names if norm_key(n) not in cached][:MAX_PER_CALL]
    if not unknown:
        return {}

    data = llm.json(_prompt(unknown), system=SYSTEM, max_tokens=1500)
    if not isinstance(data, dict):
        return {}

    valid = {c.code for c in cats.CATEGORIES if not c.is_income}
    learned: dict[str, str] = {}
    for item in data.get("items") or []:
        try:
            name = str(item["name"]).strip()
            code = str(item["category"]).strip()
        except (KeyError, TypeError):
            continue
        if not name or code not in valid or code == cats.OTHER:
            continue
        key = norm_key(name)
        if not key or key in cached:
            continue
        learned[key] = code
        session.add(
            MerchantRule(
                user_id=user_id,
                match_type=MatchType.EXACT,
                pattern=key,
                category_code=code,
                priority=50,
            )
        )
    if learned:
        session.flush()
        log.info("языковая модель разобрала продавцов: %d", len(learned))
    return learned
