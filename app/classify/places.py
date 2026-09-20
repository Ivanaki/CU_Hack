"""Мои места: человек один раз называет продавца по-своему.

Живой случай: голосом сказано «350 на узбечку», а в выписке тот же ларёк
проходит как «InterFood». Человеку нужно одно название на оба следа, и
нужно, чтобы оно стояло на всех тратах — и на прошлых, и на будущих.

Где это живёт
-------------
В таблице merchant_rules, рядом с правилами категоризации, а не в соседней
таблице. Причина одна: «продавец -> что это значит» — уже существующее
понятие, у него уже есть владелец (user_id), нормализованный ключ
(pattern) и категория. Соседняя таблица завела бы второй pattern, второй
способ его нормализовать и второе место, где их надо держать в согласии, —
а расходились бы они молча.

Личное правило отличается от словарного значением match_type:

    EXACT    — словарь: точное совпадение ключа, так их пишет языковая
               модель в llm_category.py;
    CONTAINS — моё место: сопоставление по смыслу (см. ниже).

Поэтому выборки не пересекаются: словарь языковой модели не видит личных
правил и не переучивает их, а «мои места» не показывают человеку то, что
он не заводил.

Пустой category_code означает «человек назвал место, но категорию не
трогал». NULL там был бы честнее, но колонка в уже задеплоенной базе
объявлена NOT NULL, а менять это в SQLite можно только пересборкой
таблицы — ради одного оттенка пустоты того не стоит.

Как сопоставляется «то же место»
--------------------------------
«InterFood», «INTERFOOD 12» и «Интерфуд» — один ларёк. Точное совпадение
ключей этого не даёт, поэтому поверх norm_key (единственная нормализация в
проекте, та же, что у счетов и контрагентов) добавлены три вещи:

  * транслитерация кириллицы в латиницу — иначе «Интерфуд» и «InterFood»
    лежат в разных алфавитах и не сравнимы в принципе;
  * схлопывание удвоенных букв — «interfood» и «interfud» после него
    отличаются одной буквой, а не тремя;
  * сравнение по значимым словам с допуском на опечатку и падеж, чтобы
    «на узбечку» нашло «Узбечка», а номер терминала не мешал.

Допуск подобран так, чтобы «Магнит» и «Магнат» остались разными местами:
похожесть у них 0.83, порог — 0.84. Короткие слова сравниваются только
точно: на трёх буквах любая мера похожести врёт.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classify import categories as cats
from app.ledger.accounts import norm_key
from app.models import MatchType, MerchantRule

# Личное правило важнее словарного: человек знает про своё место больше,
# чем языковая модель про строку из выписки.
PLACE_PRIORITY = 10

# Ниже этой длины сравниваем только точно.
MIN_FUZZY = 4
# Порог похожести. См. «Магнит»/«Магнат» в докстринге модуля.
SIMILAR_ENOUGH = 0.84

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y",
    "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


class PlaceRejected(ValueError):
    """Место не сохранено: человеку есть что поправить в том, что он прислал."""


def pattern_of(merchant: str) -> str:
    """Ключ правила. Тот же norm_key, что у всех остальных правил в базе.

    Хранить транслитерированный ключ было бы соблазнительно, но тогда
    pattern в merchant_rules значил бы разное у разных строк одной таблицы.
    Транслитерация — забота сравнения, а не хранения.
    """
    return norm_key(merchant)


def place_key(text: str) -> str:
    """Ключ сравнения: нормализация, транслитерация, схлопывание удвоений."""
    key = norm_key(text)
    if not key:
        return ""
    out = "".join(_TRANSLIT.get(ch, ch) for ch in key)
    # «interfood» -> «interfod»: удвоение в латинице и его отсутствие в
    # кириллице — самая частая разница между двумя написаниями одного слова.
    out = re.sub(r"(.)\1+", r"\1", out)
    return re.sub(r"\s+", " ", out).strip()


def _tokens(key: str) -> list[str]:
    """Значимые слова ключа.

    Номера терминалов и предлоги выкидываем: «INTERFOOD 12» и «на узбечку»
    должны опознаваться по тому единственному слову, которое называет место.
    """
    return [t for t in key.split() if len(t) >= 3 and not t.isdigit()]


def _close(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) < MIN_FUZZY or len(b) < MIN_FUZZY:
        return False
    if a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= SIMILAR_ENOUGH


def same_place(pattern: str, merchant: str) -> bool:
    """Одно ли это место. Сравнение симметрично по написанию, не по длине."""
    left, right = place_key(pattern), place_key(merchant)
    if not left or not right:
        return False
    if left == right:
        return True
    wanted = _tokens(left)
    found = _tokens(right) or right.split()
    if wanted and all(any(_close(w, f) for f in found) for w in wanted):
        return True
    return _close(left, right)


def user_places(session: Session, user_id: int) -> list[MerchantRule]:
    """Названия мест этого человека. Чужие и словарные сюда не попадают."""
    rules = session.scalars(
        select(MerchantRule)
        .where(
            MerchantRule.user_id == user_id,
            MerchantRule.match_type == MatchType.CONTAINS,
        )
        .order_by(MerchantRule.pattern)
    ).all()
    return [rule for rule in rules if rule.title]


def match(rules: list[MerchantRule], merchant: str) -> MerchantRule | None:
    """Правило для этого написания продавца. Точное совпадение — вперёд.

    Иначе «Пятёрочка» и «Пятёрочка Экспресс», заведённые как разные места,
    зависели бы от порядка строк в базе.

    Сравниваем и с тем, как место подписано в выписке, и с тем, как человек
    его назвал. Второе — не вольность: «350 на узбечку» сказано его же
    словом, и не узнать в нём «Узбечку» значило бы потерять ровно тот
    случай, ради которого всё это делается. Выдумкой было бы обратное —
    самим решить, что «узбечка» и «InterFood» одно место; это сказал он.
    """
    if not merchant:
        return None
    key = place_key(merchant)
    for rule in rules:
        if key in (place_key(rule.pattern), place_key(rule.title)):
            return rule
    for rule in rules:
        if same_place(rule.pattern, merchant) or same_place(rule.title, merchant):
            return rule
    return None


def save(
    session: Session, user_id: int, merchant: str, title: str, category: str | None
) -> MerchantRule:
    """Заводит или обновляет название места. Деньги при этом не меняются."""
    pattern = pattern_of(merchant)
    if not pattern:
        raise PlaceRejected("не понял, какое место переименовать")
    name = re.sub(r"\s+", " ", title or "").strip()
    if not name:
        raise PlaceRejected("название места не может быть пустым")
    code = (category or "").strip()
    if code and code not in cats.BY_CODE:
        raise PlaceRejected(f"нет такой категории: {code}")
    if code and cats.is_income_code(code):
        raise PlaceRejected("место — это трата, категория дохода ему не подходит")

    rule = session.scalar(
        select(MerchantRule).where(
            MerchantRule.user_id == user_id,
            MerchantRule.match_type == MatchType.CONTAINS,
            MerchantRule.pattern == pattern,
        )
    )
    if rule is None:
        rule = MerchantRule(
            user_id=user_id,
            match_type=MatchType.CONTAINS,
            pattern=pattern,
            category_code=code,
            priority=PLACE_PRIORITY,
        )
        session.add(rule)
    else:
        rule.category_code = code
    rule.title = name[:200]
    session.flush()
    return rule


def drop(session: Session, user_id: int, pattern: str) -> bool:
    """Убирает название. False — такого места у этого человека нет."""
    rule = session.scalar(
        select(MerchantRule).where(
            MerchantRule.user_id == user_id,
            MerchantRule.match_type == MatchType.CONTAINS,
            MerchantRule.pattern == pattern_of(pattern),
        )
    )
    if rule is None:
        return False
    session.delete(rule)
    session.flush()
    return True
