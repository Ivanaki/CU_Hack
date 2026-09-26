"""Обогащение сырых строк до классификации.

Разбирает описание операции: кто контрагент (человек или магазин), что
написано в назначении платежа, к какой категории относится MCC. Ничего не
решает про смысл операции — только готовит признаки, на которых потом
работают правила.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classify import categories as cats
from app.classify import places
from app.classify.phone_identities import identities, transfer_phone
from app.ledger.accounts import AccountRegistry, norm_key
from app.models import Counterparty, CounterpartyKind, RawTransaction

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

# Некоторые банки подставляют в поле отправителя/получателя «Мой номер»
# вместо ФИО. Это явная пометка собственного телефона только в контексте
# перевода, а не произвольное упоминание номера в покупке или оплате связи.
_OWN_NUMBER = re.compile(
    r"\b(?:с|со|от|на|по)\s+(?:номер(?:а|у)?\s*)?[«\"'„]?\s*мой\s+номер\b",
    re.IGNORECASE,
)
_MONEY_MOVEMENT = re.compile(r"перевод\w*|сбп|зачислени\w*|пополнени\w*", re.IGNORECASE)
_TBANK_CONTRACT_TRANSFER = re.compile(
    r"\b(?:внутренний\s+перевод\s+на\s+договор|перевод\s+с\s+договора)\b",
    re.IGNORECASE,
)

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


def purpose_tags(text: str, *, bank: str = "") -> list[str]:
    found = [tag for rx, tag in _COMPILED if rx.search(text or "")]
    if own_number_transfer(text) or tbank_contract_transfer(text, bank):
        found.append("self")
    # dict.fromkeys сохраняет порядок и убирает повторы
    return list(dict.fromkeys(found))


def own_number_transfer(text: str) -> bool:
    """Банк прямо обозначил свой номер как реквизит перевода."""
    return bool(_OWN_NUMBER.search(text or "") and _MONEY_MOVEMENT.search(text or ""))


def tbank_contract_transfer(text: str, bank: str) -> bool:
    """Перевод между картой и своим договором в формулировке Т-Банка."""
    bank_key = norm_key(bank).replace(" ", "")
    return bank_key in {"тбанк", "тинькофф", "tbank", "tinkoff"} and bool(
        _TBANK_CONTRACT_TRANSFER.search(text or "")
    )


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


_ANSWER_CATEGORY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(?:доставк\w*\s+ед\w*|заказ\w*\s+ед\w*)\b", "delivery"),
    (r"\b(?:продукт\w*|супермаркет\w*|магазин\w*\s+ед\w*)\b", "groceries"),
    (r"\b(?:кафе|ресторан\w*|обед\w*|ужин\w*|кофейн\w*)\b", "cafe"),
    (r"\b(?:такси|таксист\w*)\b", "taxi"),
    (r"\b(?:общественн\w*\s+транспорт\w*|метро|автобус\w*|проезд\w*)\b", "transport"),
    (r"\b(?:бензин\w*|заправк\w*|автомобил\w*|машин\w*|парковк\w*)\b", "car"),
    (r"\b(?:квартир\w*|жиль\w*|аренд\w*|коммунал\w*|жкх)\b", "housing"),
    (r"\b(?:связ\w*|интернет\w*|мобильн\w*\s+тариф\w*)\b", "connection"),
    (r"\b(?:здоров\w*|аптек\w*|лекарств\w*|врач\w*|лечени\w*)\b", "health"),
    (r"\b(?:красот\w*|косметик\w*|парикмахер\w*|маникюр\w*)\b", "beauty"),
    (r"\b(?:одежд\w*|обув\w*|куртк\w*|кроссовк\w*)\b", "clothes"),
    (r"\b(?:техник\w*|гаджет\w*|телефон\w*|ноутбук\w*)\b", "electronics"),
    (r"\b(?:товар\w*\s+для\s+дом\w*|мебел\w*|посуда|ремонт\w*\s+(?:дом\w*|квартир\w*))\b", "home"),
    (r"\b(?:развлечени\w*|кино\w*|театр\w*|концерт\w*)\b", "entertainment"),
    (r"\b(?:подписк\w*|абонемент\w*\s+на\s+сервис\w*)\b", "subscriptions"),
    (r"\b(?:образовани\w*|учеб\w*|курс\w*)\b", "education"),
    (r"\b(?:путешестви\w*|отел\w*|гостиниц\w*|авиабилет\w*)\b", "travel"),
    (r"\b(?:спорт\w*|фитнес\w*|тренировк\w*)\b", "sport"),
    (r"\b(?:питом\w*|кошк\w*|собак\w*|ветеринар\w*)\b", "pets"),
    (r"\b(?:подар\w*|цвет\w*|букет\w*)\b", "gifts"),
    (r"\b(?:дет\w*|ребен\w*|игрушк\w*)\b", "kids"),
    (r"\b(?:комисси\w*|процент\w*\s+банк\w*)\b", "fees"),
    (r"\b(?:налог\w*|штраф\w*)\b", "taxes"),
)


def category_from_answer(text: str) -> str | None:
    """Распознаёт названия категорий и обычные слова в ответе человека."""
    normalized = norm_key(text)
    for pattern, code in _ANSWER_CATEGORY_PATTERNS:
        if re.search(pattern, normalized):
            return code
    return category_from_text(text)


def enrich(session: Session, reg: AccountRegistry, rows: list[RawTransaction]) -> None:
    """Проставляет контрагента, теги назначения и подсказку категории."""
    _enrich_basic(session, reg, rows)
    _enrich_with_places(session, rows)
    _enrich_with_llm(session, reg, rows)


def _enrich_with_places(session: Session, rows: list[RawTransaction]) -> None:
    """Категория из «моих мест» — если человек её назвал.

    Идёт после MCC и словаря и перебивает их: код MCC ставит банк, а место
    назвал человек, и он про свой ларёк знает больше. Перебить человека
    может только решение по конкретной операции — оно применяется позже,
    уже в правилах классификации.

    Название места здесь не трогаем: оно принадлежит операции, а не сырой
    строке, и ставится после того, как операции собраны (post_places).
    """
    spends = [row for row in rows if row.amount < 0]
    if not spends:
        return
    rules = [
        rule
        for rule in places.user_places(session, spends[0].user_id)
        if rule.category_code
    ]
    if not rules:
        return
    for row in spends:
        rule = places.match(rules, clean_merchant(row.counterparty_raw or row.description))
        if rule is not None:
            row.mcc_category = rule.category_code


def _enrich_basic(
    session: Session, reg: AccountRegistry, rows: list[RawTransaction]
) -> None:
    phone_rules = identities(session, reg.user_id)
    for row in rows:
        row.counterparty_id = None
        text = f"{row.description} {row.counterparty_raw}".strip()
        row.purpose_tags = purpose_tags(text, bank=row.account.bank or "")

        name = extract_person_name(row.counterparty_raw or row.description)
        kind = guess_counterparty_kind(text, name)
        display = name or clean_merchant(row.counterparty_raw or row.description)
        number = transfer_phone(row)
        named = phone_rules.get(number) if number else None
        if named is not None and named.kind == "contact":
            # Номер — ключ личности. Два друга с одинаковым именем не
            # разделяют долги и вопросы, а переименование обновляет прошлое.
            key = f"phone:{number}"
            cp = session.scalar(select(Counterparty).where(
                Counterparty.user_id == row.user_id, Counterparty.norm_key == key
            ))
            if cp is None:
                cp = Counterparty(
                    user_id=row.user_id, kind=CounterpartyKind.PERSON,
                    norm_key=key, display_name=named.name,
                )
                session.add(cp)
                session.flush()
            else:
                cp.display_name = named.name
                cp.kind = CounterpartyKind.PERSON
            row.counterparty_id = cp.id
        elif display:
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
