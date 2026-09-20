"""Обогащение сырых строк до классификации.

Разбирает описание операции: кто контрагент (человек или магазин), что
написано в назначении платежа, к какой категории относится MCC. Ничего не
решает про смысл операции — только готовит признаки, на которых потом
работают правила.
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.classify import categories as cats
from app.ledger.accounts import AccountRegistry, norm_key
from app.models import CounterpartyKind, RawTransaction

# --- назначение платежа ----------------------------------------------------
# Теги, а не категории: одно и то же слово может значить разное в зависимости
# от направления денег. "за обед" при входящем — возврат доли, при исходящем —
# просто трата.
PURPOSE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bдолг\w*\b|\bзайм\w*\b|\bв\s+долг\b|\bдолжок\b|\bзанял\b|\bодолж\w+", "debt"),
    (r"\bвозвра\w+|\bотдаю\b|\bвернул\w*\b|\bвозме\w+", "return"),
    (r"\bза\s+(ужин|обед|завтрак|еду|кафе|ресторан|пиццу|суши|бар)\b", "share_meal"),
    (r"\bза\s+(такси|бензин|дорогу|поездку)\b", "share_ride"),
    (r"\bза\s+(квартиру|аренду|жиль[её]|коммуналк\w+)\b", "share_rent"),
    (r"\bсбор\w*\b|\bскидыва\w+|\bскинул\w*\b|\bдол[яю]\b|\bпополам\b|\bна\s+всех\b", "split"),
    (r"\bподар\w+|\bдр\b|\bна\s+день\s+рожд\w+", "gift"),
    (r"\bсебе\b|\bмежду\s+свои\w+|\bна\s+свою\s+карт\w+|\bсвой\s+сч[её]т\b", "self"),
    (r"\bснятие\b|\bбанкомат\w*\b|\batm\b|\bвыдача\s+наличн\w+", "atm"),
    (r"\bпополнени\w+|\bвнесени\w+\s+наличн\w+", "topup"),
    (r"\bзарплат\w+|\bаванс\b|\bз/?п\b|\bоплата\s+труда\b", "salary"),
    (r"\bк[еэ]шб[еэ]к\w*\b|\bcashback\b|\bбонус\w*\b", "cashback"),
    (r"\bпроцент\w+\s+по\s+вклад\w+|\bначислени\w+\s+процент\w+", "interest"),
    (r"\bкомисси\w+", "fee"),
    (r"\bперевод\b", "transfer"),
    (r"\bоплата\s+счета\b|\bсч[её]т\s+на\s+компанию\b", "bill"),
)

_COMPILED = tuple((re.compile(p, re.IGNORECASE), tag) for p, tag in PURPOSE_PATTERNS)

# ФИО ищется внутри описания, а не сверяется с ним целиком: в выписке
# Т-Банка отдельной колонки контрагента нет, и имя тонет в тексте вида
# "Перевод от Иван П. возврат части долга". Без этого каждая строка
# получала бы своего контрагента, и долг никогда бы не сошёлся с возвратом.
_PERSON_RE = re.compile(
    r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ]\.(?:\s*[А-ЯЁ]\.)?"  # Иван П. / Иванов И. И.
    r"|\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}\b"  # Иванов Иван Иванович
    r"|\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}ов(?:а)?\b",  # Иван Петров / Анна Петрова
    re.UNICODE,
)
_TRANSFER_HINT = re.compile(r"перевод|сбп|c2c|card2card|p2p", re.IGNORECASE)

# Слова, которые описывают смысл платежа, а не того, кому он адресован.
# Без их удаления "DNS SHOP" и "DNS SHOP возврат покупки" оказываются
# разными продавцами, и возврат товара не находит свою покупку.
_PURPOSE_NOISE = re.compile(
    r"\b(возврат\w*|покупк\w+|оплат\w+|перевод\w*|платеж\w*|списани\w+|зачислени\w+|"
    r"пополнени\w+|отмена|операци\w+|по\s+карте|с\s+карты|на\s+карту|за\s+\w+|"
    r"через\s+\w+|сбп|заказ\s*№?\s*\d*|чек\s*№?\s*\d*)\b",
    re.IGNORECASE,
)


def purpose_tags(text: str) -> list[str]:
    found = [tag for rx, tag in _COMPILED if rx.search(text or "")]
    # dict.fromkeys сохраняет порядок и убирает повторы
    return list(dict.fromkeys(found))


def extract_person_name(text: str) -> str | None:
    """Достаёт имя физлица из описания перевода, если оно там есть."""
    if not text:
        return None
    match = _PERSON_RE.search(text)
    if match is None:
        return None
    name = re.sub(r"\s+", " ", match.group(0)).strip(" ,;:-")
    # Точку после инициала оставляем: «Иван П.», а не «Иван П».
    if not re.search(r"\b[А-ЯЁ]\.$", name):
        name = name.rstrip(".")
    # Отсекаем слова-паразиты, случайно похожие на имя.
    if norm_key(name).split()[0] in _NOT_NAMES:
        return None
    return name


# Слова с заглавной буквы, которые в выписке стоят на месте имени, но именем
# не являются. Без этого "Перевод Ольга" и "Оплата Услуг" путаются.
_NOT_NAMES = frozenset(
    {"перевод", "оплата", "покупка", "возврат", "зачисление", "списание", "пополнение",
     "аренда", "комиссия", "проценты", "кэшбэк", "снятие", "выдача", "услуги", "сбп"}
)


def guess_counterparty_kind(text: str, name: str | None) -> CounterpartyKind:
    if name is not None:
        return CounterpartyKind.PERSON
    if _TRANSFER_HINT.search(text or ""):
        return CounterpartyKind.PERSON
    return CounterpartyKind.MERCHANT


def clean_merchant(text: str) -> str:
    """Оставляет от строки только название продавца.

    Города, коды терминалов, номера заказов и описание смысла платежа
    отбрасываются: продавец должен называться одинаково и в покупке, и в
    возврате, иначе их не связать.
    """
    out = re.sub(r"\b[A-Z]{2,3}\s*\d{3,}\b", " ", text or "")
    out = re.sub(r"\b\d{4,}\b", " ", out)
    out = re.sub(r"\b(RUS|MOSCOW|MOSKVA|SPB|G\.|ГОР\.)\b", " ", out, flags=re.IGNORECASE)
    out = _PURPOSE_NOISE.sub(" ", out)
    out = re.sub(r"[*_>№]+", " ", out)
    cleaned = re.sub(r"\s+", " ", out).strip(" .,;:-")
    return cleaned or re.sub(r"\s+", " ", (text or "")).strip()


def category_from_mcc(mcc: str | None) -> str | None:
    if not mcc:
        return None
    return cats.MCC_CATEGORY.get(str(mcc).strip().zfill(4))


def category_from_text(text: str) -> str | None:
    low = norm_key(text)
    if not low:
        return None
    for keyword, code in cats.MERCHANT_KEYWORDS:
        if norm_key(keyword) in low:
            return code
    return None


def enrich(session: Session, reg: AccountRegistry, rows: list[RawTransaction]) -> None:
    """Проставляет контрагента, теги назначения и подсказку категории."""
    _enrich_basic(session, reg, rows)
    _enrich_with_llm(session, reg, rows)


def _enrich_basic(
    session: Session, reg: AccountRegistry, rows: list[RawTransaction]
) -> None:
    for row in rows:
        text = f"{row.description} {row.counterparty_raw}".strip()
        row.purpose_tags = purpose_tags(text)

        name = extract_person_name(row.counterparty_raw or row.description)
        kind = guess_counterparty_kind(text, name)
        display = name or clean_merchant(row.counterparty_raw or row.description)
        if display:
            row.counterparty_id = reg.counterparty(display, kind).id

        # Категорию по тексту ищем и для переводов физлицам: "Аренда квартиры
        # Смирнова Е. В." — это жильё, а не загадочный перевод человеку.
        row.mcc_category = category_from_mcc(row.mcc) or category_from_text(text)


def _enrich_with_llm(
    session: Session, reg: AccountRegistry, rows: list[RawTransaction]
) -> None:
    """Дораскрашивает то, что не взяли MCC и словарь.

    Необязательный шаг: кейс прямо говорит, что языковая модель тут
    упрощает жизнь, но не требуется. Выключается через LLM_PROVIDER=none,
    и тогда непонятные траты просто останутся в «Прочем» — честно и видно
    в проценте осознанности.
    """
    from app.classify.llm_category import classify_merchants, known_rules  # noqa: PLC0415

    pending = [r for r in rows if r.amount < 0 and not r.mcc_category]
    if not pending:
        return

    # Сначала применяем то, что уже выучено: за известного продавца
    # платить второй раз незачем.
    rules = known_rules(session, list({r.user_id for r in pending})[0])
    names: list[str] = []
    for row in pending:
        raw = clean_merchant(row.counterparty_raw or row.description)
        key = norm_key(raw)
        if key in rules:
            row.mcc_category = rules[key]
        elif raw:
            names.append(raw)

    still_unknown = [r for r in pending if not r.mcc_category]
    if not still_unknown or not names:
        return

    learned = classify_merchants(
        session, still_unknown[0].user_id, list(dict.fromkeys(names))
    )
    if not learned:
        return
    for row in still_unknown:
        key = norm_key(clean_merchant(row.counterparty_raw or row.description))
        if key in learned:
            row.mcc_category = learned[key]
