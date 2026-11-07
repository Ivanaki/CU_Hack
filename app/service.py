"""Сценарии использования. Один слой на бота и на Mini App.

Бот и HTTP-API — это две оболочки вокруг одних и тех же функций. Логика
живёт здесь, чтобы «ответить на вопрос одним тапом» вело себя одинаково,
откуда бы тап ни пришёл.
"""

from __future__ import annotations

import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.analytics import reports
from app.analytics.periods import Period, month, week
from app.classify import categories as cats
from app.classify import places
from app.classify.enrich import category_from_text, purpose_tags
from app.classify.pipeline import DecisionKind, reclassify
from app.ingest.base import ParsedRow
from app.ingest.service import ImportResult, add_manual, import_file, import_images
from app.models import (
    Counterparty,
    DailyActivity,
    Decision,
    ImportBatch,
    Money,
    Operation,
    OperationSource,
    RawTransaction,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    User,
)


@dataclass(slots=True)
class BatchTrace:
    """Что получилось из одной конкретной загрузки.

    Отличается от полей IngestOutcome.operations/questions тем, что считает
    не состояние книги после пересчёта, а вклад именно этого файла: человек
    спрашивает «что распозналось из того, что я сейчас скинул», а не
    «сколько всего у меня операций».
    """

    batch_id: int
    added_rows: int
    duplicates: int
    operations: int
    questions: int
    # Сколько операций файл показал всего, включая те, что уже были в
    # книге. Повторная присылка того же скриншота — обычное дело (человек
    # фотографирует список раз в неделю, пересечение неизбежно), и без
    # этого числа такая загрузка выглядит как «ничего не распозналось».
    recognized: int = 0


@dataclass(slots=True)
class IngestOutcome:
    imported: ImportResult | None
    operations: int
    questions: int
    unresolved: Money
    error: str = ""
    # Что именно распознали на картинке: statement, receipt или other.
    image_kind: str = ""
    # След загрузки: по нему видно, что дал именно этот файл.
    trace: BatchTrace | None = None

    @property
    def ok(self) -> bool:
        return not self.error


# --- пользователь и активность --------------------------------------------


def get_or_create_user(session: Session, tg_id: int, display_name: str = "") -> User:
    """Находит человека или заводит его. Безопасна при одновременных запросах.

    Mini App при открытии дёргает четыре ручки разом — сводку, геймификацию,
    аналитику и операции. У нового человека все четыре не находят его и все
    четыре пытаются завести: одна выигрывает, остальные падают на уникальном
    индексе, и человек на первом же экране видит «API недоступен». Проверка
    «нет — добавь» без обработки гонки здесь не работает по определению:
    между SELECT и INSERT успевает вклиниться соседний запрос.

    Поэтому вставка идёт в SAVEPOINT: проиграли гонку — откатываем только её
    и перечитываем чужую запись, вместо того чтобы валить весь запрос.
    """
    user = session.scalar(select(User).where(User.tg_id == tg_id))
    if user is not None:
        if display_name and not user.display_name:
            user.display_name = display_name
        return user

    try:
        with session.begin_nested():
            user = User(tg_id=tg_id, display_name=display_name)
            session.add(user)
            session.flush()
        return user
    except IntegrityError:
        # Успел сосед. Его запись — та же самая, спорить не о чем.
        user = session.scalar(select(User).where(User.tg_id == tg_id))
        if user is None:  # не гонка, а что-то другое — молчать нельзя
            raise
        if display_name and not user.display_name:
            user.display_name = display_name
        return user


# --- персональный секрет загрузки ------------------------------------------
#
# Файлы со всех телефонов приходят на один адрес, поэтому «кто прислал»
# решает не URL, а секрет в запросе. Логика живёт здесь, а не в ручке:
# ссылку выдают и бот, и Mini App, и оба обязаны выдавать одну и ту же.

# 32 байта — 256 бит энтропии. Перебрать по сети нельзя, а значит защита от
# перебора не нужна как таковая: её роль выполняет длина.
UPLOAD_TOKEN_BYTES = 32
# Префикс не секретен и ничего не сообщает — он нужен, чтобы токен узнавался
# глазами, если всё-таки окажется там, где ему не место.
UPLOAD_TOKEN_PREFIX = "fm_"
# Короче этого токеном быть не может: отсекаем мусор до похода в базу.
UPLOAD_TOKEN_MIN_LEN = 16


def _new_upload_token() -> str:
    # Только secrets: random предсказуем по нескольким выдачам, а uuid4 —
    # это идентификатор, у него фиксированы биты версии и варианта.
    # Ничего производного от tg_id или времени: иначе перебор секрета
    # сводится к перебору коротких чисел, ради ухода от которого всё и затеяно.
    return UPLOAD_TOKEN_PREFIX + secrets.token_urlsafe(UPLOAD_TOKEN_BYTES)


def issue_upload_token(session: Session, user: User) -> str:
    """Ссылка человека на загрузку. Создаётся при первом обращении."""
    if not user.upload_token:
        user.upload_token = _new_upload_token()
        user.upload_token_at = datetime.now()
        session.flush()
    return user.upload_token


def rotate_upload_token(session: Session, user: User) -> str:
    """Отзыв: старая ссылка перестаёт работать в тот же момент.

    Токен в записи ровно один, поэтому перевыпуск и есть отзыв — отдельного
    «отключить» не нужно, а человеку не приходится думать, что именно из
    двух действий ему нужно.
    """
    user.upload_token = _new_upload_token()
    user.upload_token_at = datetime.now()
    session.flush()
    return user.upload_token


def user_by_upload_token(session: Session, token: str) -> User | None:
    """Кому принадлежит секрет. None — не признали.

    Индекс только сужает поиск, решение принимает постоянное по времени
    сравнение байтов: сравнение в SQL зависит от коллации (в MySQL она по
    умолчанию регистронезависимая), и считать его проверкой секрета
    нельзя. Байты, а не строки, потому что токен приходит из запроса и
    бывает каким угодно — на строках сравнение падает на не-ASCII.
    """
    raw = (token or "").strip()
    if len(raw) < UPLOAD_TOKEN_MIN_LEN:
        return None
    user = session.scalar(select(User).where(User.upload_token == raw))
    if user is None or not user.upload_token:
        return None
    if not hmac.compare_digest(raw.encode("utf-8"), user.upload_token.encode("utf-8")):
        return None
    return user


# --- прослеживаемость: файл <-> операции ------------------------------------


def _batch_rows(batch: ImportBatch):
    """Сырые строки, которые показал этот файл: свои и уже известные.

    Строка-дубль в базу не пишется — иначе сломалась бы дедупликация, —
    поэтому по batch_id её не найти. Но её хеш мы запомнили в самой
    загрузке, и по нему находится та строка, что приехала раньше. Без
    этого повторная присылка того же скриншота показывала бы пустой
    список: «файл приехал, ничего не вышло» — при восьми его операциях
    на экране.
    """
    known = list(batch.duplicate_hashes or [])
    seen = RawTransaction.batch_id == batch.id
    if known:
        seen = or_(seen, RawTransaction.dedup_hash.in_(known))
    return RawTransaction.user_id == batch.user_id, seen


def batch_operations(session: Session, batch: ImportBatch) -> list[Operation]:
    """Операции, которые показала эта загрузка.

    Идём через RawTransaction, а не через колонку в Operation: сырая
    строка иммутабельна и переживает пересчёт, а операции при каждом
    пересчёте пересобираются с нуля.
    """
    from_batch = select(RawTransaction.operation_id).where(
        *_batch_rows(batch), RawTransaction.operation_id.is_not(None)
    )
    return list(
        session.scalars(
            select(Operation)
            .where(Operation.user_id == batch.user_id, Operation.id.in_(from_batch))
            .order_by(Operation.occurred_at.desc(), Operation.id)
        ).all()
    )


def batch_questions(session: Session, batch: ImportBatch) -> list[ReviewItem]:
    """Открытые вопросы, которые подняла эта загрузка.

    Якорь вопроса — dedup_hash сырой строки, а не номер операции: операции
    пересоздаются, хеш нет. Поэтому связь «файл -> вопрос» не рвётся после
    ответа на соседний вопрос.
    """
    anchors = select(RawTransaction.dedup_hash).where(*_batch_rows(batch))
    return list(
        session.scalars(
            select(ReviewItem)
            .where(
                ReviewItem.user_id == batch.user_id,
                ReviewItem.status == ReviewStatus.OPEN,
                ReviewItem.anchor_hash.in_(anchors),
            )
            .order_by(ReviewItem.priority.desc(), ReviewItem.id)
        ).all()
    )


def trace_batch(session: Session, result: ImportResult) -> BatchTrace:
    """Вклад одной загрузки в книгу. Считается после пересчёта."""
    batch = result.batch
    operations = int(
        session.scalar(
            select(func.count(func.distinct(RawTransaction.operation_id))).where(
                RawTransaction.batch_id == batch.id,
                RawTransaction.operation_id.is_not(None),
            )
        )
        or 0
    )
    return BatchTrace(
        batch_id=batch.id,
        added_rows=len(result.new_rows),
        duplicates=result.duplicates,
        operations=operations,
        questions=len(batch_questions(session, batch)),
        # «Добавилось» и «видно по файлу» — разные числа, и подменять одно
        # другим нельзя: первое отвечает «что изменилось», второе — «что я
        # прислал». На повторной присылке они расходятся как 0 и 8.
        recognized=len(batch_operations(session, batch)),
    )


def owner_of_file(session: Session, stored_as: str) -> int | None:
    """Чей это файл на диске. None — ничей, такой записи у нас нет.

    Единственная связь между именем файла и человеком — запись о
    загрузке: имя генерируем мы и больше нигде не храним.
    """
    if not stored_as:
        return None
    return session.scalar(
        select(ImportBatch.user_id).where(ImportBatch.stored_as == stored_as).limit(1)
    )


def remember_upload(
    session: Session,
    user_id: int,
    *,
    stored_as: str,
    content_type: str = "",
    size_bytes: int = 0,
    channel: str = "",
    filename: str = "",
) -> ImportBatch | None:
    """Записывает «этот файл принадлежит этому человеку» без разбора.

    Нужна там, где разбор не состоялся: модель не ответила, файл не
    картинка, разбор выключен флагом. Файл при этом уже лежит в закрытом
    каталоге, а закрытый каталог отдаёт только хозяину — и если записи о
    загрузке нет, хозяина не существует, и человек не может открыть
    собственный файл. Пустой батч честно говорит «файл приехал, операций
    из него нет», ровно как при нераспознанной картинке.

    Идемпотентна: повторный вызов для того же имени ничего не создаёт.
    """
    if not stored_as:
        return None
    existing = session.scalar(
        select(ImportBatch).where(ImportBatch.stored_as == stored_as).limit(1)
    )
    if existing is not None:
        return existing
    batch = ImportBatch(
        user_id=user_id,
        source="upload",
        filename=filename or stored_as,
        rows_total=0,
        rows_new=0,
        rows_duplicate=0,
        stored_as=stored_as,
        content_type=content_type,
        size_bytes=size_bytes,
        channel=channel,
    )
    session.add(batch)
    session.flush()
    return batch


def owned_stored_names(session: Session) -> list[str]:
    """Имена всех файлов, у которых есть хозяин."""
    return [
        name
        for name in session.scalars(
            select(ImportBatch.stored_as).where(ImportBatch.stored_as != "").distinct()
        ).all()
        if name
    ]


def recent_imports(session: Session, user_id: int, limit: int = 20) -> list[ImportBatch]:
    return list(
        session.scalars(
            select(ImportBatch)
            .where(ImportBatch.user_id == user_id)
            .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
            .limit(limit)
        ).all()
    )


def user_batch(session: Session, user_id: int, batch_id: int) -> ImportBatch | None:
    batch = session.get(ImportBatch, batch_id)
    # Чужая загрузка для этого пользователя не существует — ровно так же,
    # как и несуществующая: по ответу нельзя узнать, что она вообще есть.
    if batch is None or batch.user_id != user_id:
        return None
    return batch


def batches_by_operation(
    session: Session, user_id: int, operation_ids: list[int]
) -> dict[int, list[ImportBatch]]:
    """Обратное направление: из каких файлов приехала каждая операция.

    Операция может быть склеена из строк двух разных загрузок (скриншот и
    пришедшая позже выписка), поэтому значение — список, а не одна запись.
    """
    if not operation_ids:
        return {}
    rows = session.execute(
        select(RawTransaction.operation_id, ImportBatch)
        .join(ImportBatch, RawTransaction.batch_id == ImportBatch.id)
        .where(
            RawTransaction.user_id == user_id,
            RawTransaction.operation_id.in_(operation_ids),
        )
        .order_by(ImportBatch.created_at, ImportBatch.id)
    ).all()
    found: dict[int, list[ImportBatch]] = {}
    for operation_id, batch in rows:
        bucket = found.setdefault(int(operation_id), [])
        if all(item.id != batch.id for item in bucket):
            bucket.append(batch)
    return found


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

    _advance_streak(session, user, today)


def _advance_streak(session: Session, user: User, today: date) -> None:
    """Продлевает серию за сегодняшний день."""
    if user.streak_last_date == today:
        return
    if user.streak_last_date and (today - user.streak_last_date).days == 1:
        user.streak_days += 1
    else:
        user.streak_days = 1
    user.streak_last_date = today
    user.best_streak = max(user.best_streak, user.streak_days)


def keep_streak_alive(session: Session, user: User, *, today: date | None = None) -> None:
    """Тихий день не рвёт серию.

    Продукт обещает ноль действий в день, когда всё разобрано — и было бы
    странно тем же продуктом за это наказывать. Поэтому день, в который
    система сама ни о чём не спросила, засчитывается как пройденный: серия
    про то, что человек держит свои деньги понятными, а не про то, что он
    каждый день чем-то щёлкает.

    Если же вопросы висят и человек их не трогает — серия рвётся честно,
    потому что деньги остаются неразобранными.
    """
    today = today or date.today()
    if user.streak_last_date == today:
        return
    if count_open_questions(session, user.id) > 0:
        return
    _advance_streak(session, user, today)


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
    session: Session,
    user: User,
    data: bytes,
    filename: str,
    *,
    channel: str = "",
    stored_as: str = "",
    content_type: str = "",
) -> IngestOutcome:
    try:
        result = import_file(
            session, user.id, data, filename=filename,
            channel=channel, stored_as=stored_as, content_type=content_type,
        )
    except ValueError as exc:
        return IngestOutcome(None, 0, 0, 0, error=str(exc))
    record_action(session, user, imports=1)
    report = reclassify(session, user.id)
    return IngestOutcome(
        result, report.operations, report.questions, report.unresolved_amount,
        trace=trace_batch(session, result),
    )


def ingest_screenshot(
    session: Session,
    user: User,
    images: list[bytes],
    *,
    channel: str = "",
    stored_as: str = "",
    content_type: str = "",
    filename: str = "",
) -> IngestOutcome:
    """Картинка от пользователя: скриншот приложения банка или фото чека.

    stored_as и content_type — про файл, который лежит на диске после
    /upload. Они доезжают до записи о загрузке, иначе связь «этот файл дал
    эти операции» обрывается на границе HTTP: имя на диске живёт только в
    JSON-ответе, а в базе его нет.
    """
    result, kind = import_images(
        session, user.id, images,
        channel=channel, stored_as=stored_as, content_type=content_type,
        filename=filename,
    )
    if result.batch.rows_total == 0:
        # Пустой батч остаётся в базе намеренно: «файл приехал, но ничего не
        # распозналось» — это тоже след, и без него файл выглядел бы
        # непринятым.
        return IngestOutcome(
            result, 0, 0, 0,
            error="не удалось прочитать операции на картинке",
            image_kind=kind,
            trace=trace_batch(session, result),
        )
    record_action(session, user, imports=1)
    report = reclassify(session, user.id)
    return IngestOutcome(
        result, report.operations, report.questions, report.unresolved_amount,
        image_kind=kind,
        trace=trace_batch(session, result),
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


def _name_known_place(session: Session, user_id: int, row: ParsedRow) -> None:
    """Голос и наличные попадают в ту же систему имён, что и карта.

    «350 на узбечку» — это место, названное словами, и оно должно лечь под
    то же название, что и операции с карты в этом месте. Сопоставление
    ровно то же, что у выписки (app/classify/places.py), поэтому падеж и
    предлог помехой не будут.

    Здесь только УЗНАЁМ уже названное человеком место. Нового правила не
    заводим и с банковским продавцом ничего не связываем: если человек сам
    не сказал, что «узбечка» — это «InterFood», связи нет, и выдумывать её
    нельзя. Свести их может только явное действие человека.

    Слова человека при этом не переписываем: сырая строка — это факт, и он
    остаётся таким, каким был сказан. Название ставится уже на операцию, при
    пересчёте, и поэтому снимается вместе с правилом, а не остаётся навсегда
    вписанным в запись. Здесь берём только категорию — чтобы ответ «записал:
    350 ₽, кафе» пришёл сразу, а не после первого пересчёта.
    """
    rules = [rule for rule in places.user_places(session, user_id) if rule.category_code]
    if not rules:
        return
    rule = places.match(rules, row.counterparty_raw or row.description)
    if rule is not None:
        row.category_hint = rule.category_code


def add_quick_expense(
    session: Session,
    user: User,
    text: str,
    *,
    source: OperationSource = OperationSource.MANUAL,
    when: datetime | None = None,
    channel: str = "",
    stored_as: str = "",
    content_type: str = "",
    size_bytes: int = 0,
    filename: str = "",
) -> tuple[ParsedRow | None, IngestOutcome]:
    row = parse_quick_entry(text, when=when)
    if row is None:
        return None, IngestOutcome(None, 0, 0, 0, error="не нашёл сумму в сообщении")
    _name_known_place(session, user.id, row)
    result = add_manual(
        session, user.id, [row], origin=source, channel=channel,
        stored_as=stored_as, content_type=content_type, size_bytes=size_bytes,
        filename=filename,
    )
    record_action(session, user, manual=1)
    report = reclassify(session, user.id)
    return row, IngestOutcome(
        result, report.operations, report.questions, report.unresolved_amount,
        trace=trace_batch(session, result),
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

# Код кнопки «Своими словами». Есть у любого вопроса: заранее придуманные
# варианты не покрывают всё, а «ничего не подходит» — не повод оставлять
# деньги неразобранными.
FREE_ANSWER = "free"


class AnswerRejected(ValueError):
    """Ответ не принят. Вопрос остаётся открытым: человеку есть что поправить."""


def answer_question(
    session: Session, user: User, review_id: int, code: str, text: str = ""
) -> ReviewItem | None:
    """Один тап пользователя: записывает решение и пересчитывает всё заново.

    Решение живёт отдельно от операций и переживает пересчёт — поэтому один
    и тот же вопрос никогда не задаётся дважды. text приходит только с
    ответом «своими словами» и для остальных ответов ничего не меняет.
    """
    item = session.get(ReviewItem, review_id)
    if item is None or item.user_id != user.id or item.status != ReviewStatus.OPEN:
        return None

    text = (text or "").strip()
    if code == FREE_ANSWER and not text:
        raise AnswerRejected(
            "ответ своими словами не может быть пустым: напишите, что это за операция"
        )

    if str(item.kind) == ReviewKind.UNKNOWN_PLACE:
        # Ответ про место — не решение по одной операции, а правило для
        # продавца: оно закрывает все его траты разом и остаётся на будущие.
        if not _remember_place(session, user, item, code, text):
            return None
    else:
        decisions = (
            _free_text_decisions(str(item.kind), text)
            if code == FREE_ANSWER
            else [_decision_for(str(item.kind), code)]
        )
        if decisions[0] is None:
            return None
        for kind, payload in decisions:
            _remember_decision(session, user, item.anchor_hash, kind, payload)

    item.status = ReviewStatus.RESOLVED
    item.answer_code = code
    item.resolved_at = datetime.now()
    record_action(session, user, taps=1)
    session.flush()

    reclassify(session, user.id)
    return item


def _remember_place(
    session: Session, user: User, item: ReviewItem, code: str, text: str
) -> bool:
    """Ответ «что это за место» превращается в правило для продавца.

    Кнопка даёт категорию, а названием остаётся то, как продавец подписан в
    выписке: человек не переименовывал место, он объяснил, что это. Ответ
    своими словами наоборот: его текст и есть название, а категорию из него
    вычитает тот же определитель, что работает на выписках. Категории может
    и не найтись («бейсбольный клуб»), но деньги перестают быть непонятными —
    место названо, и pipeline.post_places помечает такие траты объяснёнными.
    """
    pattern = item.anchor_hash.removeprefix("place:")
    if not pattern:
        return False
    known = session.scalar(
        select(Counterparty).where(
            Counterparty.user_id == user.id, Counterparty.norm_key == pattern
        )
    )
    shown = known.display_name if known is not None else pattern

    if code == FREE_ANSWER:
        title, category = text[0].upper() + text[1:], category_from_text(text)
    elif code in cats.BY_CODE and not cats.is_income_code(code):
        title, category = shown, code
    else:
        return False  # кнопки такого кода у этого вопроса нет

    try:
        places.save(session, user.id, pattern, title, category)
    except places.PlaceRejected:
        return False
    return True


def _decision_for(question_kind: str, code: str) -> tuple[str, dict] | None:
    """Обычный ответ кнопкой: какое решение записать. None — код не подходит."""
    kind = _ANSWER_TO_DECISION.get(question_kind, DecisionKind.SET_KIND)
    if kind == DecisionKind.PARTICIPANTS:
        if not code.isdigit():
            return None
        return kind, {"n": int(code)}
    if kind in (DecisionKind.SET_CATEGORY, DecisionKind.CASH_ALLOCATION):
        return kind, {"category": code}
    if kind == DecisionKind.FORCE_TRANSFER:
        return (DecisionKind.NOT_TRANSFER if code == "no" else kind), {}
    return kind, {"kind": code}


def _free_text_decisions(question_kind: str, text: str) -> list[tuple[str, dict]]:
    """Во что превращается ответ своими словами у каждого вида вопроса.

    Смысл текста разбирает тот же определитель категории, что работает на
    выписках: «такси до аэропорта» попадает в «Такси» ровно так же, как
    ответ кнопкой. Если категория из слов не вычиталась, трата идёт в
    «Прочее», но помечается объяснённой — см. pipeline.post_free_text.
    """
    category = category_from_text(text)
    # Решение со словами пишется всегда и рядом с обычным: оно закрывает
    # вопрос навсегда (пересчёт не задаёт вопрос, по якорю которого есть
    # решение) и даёт операции название от человека.
    made: list[tuple[str, dict]] = [
        (DecisionKind.FREE_TEXT, {"text": text, "category": category})
    ]
    if question_kind == "shared_bill_heads":
        heads = _heads_from_text(text)
        made.append((DecisionKind.PARTICIPANTS, {"n": heads} if heads else {}))
    elif question_kind == "cash_spent_on":
        made.append((DecisionKind.CASH_ALLOCATION, {"category": category or cats.OTHER}))
    elif question_kind == "incoming_unknown":
        made.append((DecisionKind.SET_KIND, {"kind": _incoming_choice(text)}))
    elif question_kind == "is_self_transfer":
        # Человек объясняет, что это была за операция, — значит, перевод не
        # себе. Пара распадается, а ушедшие деньги становятся тратой с
        # названием от человека. Решений три: какое правило доберётся до
        # строки, зависит от того, человек на той стороне или магазин.
        made += [
            (DecisionKind.NOT_TRANSFER, {}),
            (DecisionKind.SET_KIND, {"kind": "service", "category": category or cats.OTHER}),
            (DecisionKind.SET_CATEGORY, {"category": category or cats.OTHER}),
        ]
    elif question_kind == "outgoing_to_person":
        made.append(
            (DecisionKind.SET_KIND, {"kind": "service", "category": category or cats.OTHER})
        )
    else:
        made.append((DecisionKind.SET_CATEGORY, {"category": category or cats.OTHER}))
    return made


def _incoming_choice(text: str) -> str:
    """Чем считать поступление по словам человека.

    По умолчанию доход, но «вернул за ужин» доходом называть нельзя: это
    мои же деньги, и доход от такого ответа был бы завышен.
    """
    tags = set(purpose_tags(text))
    if {"debt", "return"} & tags:
        return "debt_back"
    if {"share_meal", "share_ride", "split"} & tags:
        return "share"
    if "gift" in tags:
        return "gift"
    return "income"


def _heads_from_text(text: str) -> int:
    """Сколько человек делили счёт — если число названо цифрой."""
    match = re.search(r"\b(\d{1,2})\b", text)
    heads = int(match.group(1)) if match else 0
    return heads if 2 <= heads <= 20 else 0


def _remember_decision(
    session: Session, user: User, anchor: str, kind: str, payload: dict
) -> None:
    """Одно решение на якорь и вид: повторный ответ переписывает прежний."""
    existing = session.scalar(
        select(Decision).where(
            Decision.user_id == user.id,
            Decision.anchor_hash == anchor,
            Decision.kind == kind,
        )
    )
    if existing is None:
        session.add(
            Decision(user_id=user.id, anchor_hash=anchor, kind=kind, payload=payload)
        )
    else:
        existing.payload = payload


def skip_question(session: Session, user: User, review_id: int) -> None:
    item = session.get(ReviewItem, review_id)
    if item is not None and item.user_id == user.id:
        item.status = ReviewStatus.SKIPPED
        item.resolved_at = datetime.now()


# --- мои места -------------------------------------------------------------


@dataclass(slots=True)
class PlaceLine:
    """Одно место: как оно названо и сколько операций сейчас носят это имя."""

    pattern: str
    title: str
    category: str | None
    operations_count: int


def _place_counts(session: Session, user_id: int) -> dict[str, int]:
    """Сколько операций носит имя каждого места.

    Считаем по метке, которую ставит pipeline.post_places, а не по совпадению
    названий: два места человек вполне может назвать одинаково, а метка
    говорит, какое правило сработало на самом деле.
    """
    counts: dict[str, int] = {}
    for extra in session.scalars(
        select(Operation.extra).where(Operation.user_id == user_id)
    ).all():
        key = (extra or {}).get("place")
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def list_places(session: Session, user_id: int) -> list[PlaceLine]:
    counts = _place_counts(session, user_id)
    return [
        PlaceLine(
            pattern=rule.pattern,
            title=rule.title,
            category=rule.category_code or None,
            operations_count=counts.get(rule.pattern, 0),
        )
        for rule in places.user_places(session, user_id)
    ]


def save_place(
    session: Session, user: User, merchant: str, title: str, category: str | None
) -> PlaceLine:
    """Назвать место по-своему. Пересчитывает книгу: имя встаёт и на прошлое.

    Правило живёт в своей таблице и переживает пересчёт — поэтому оно же
    применится и ко всему, что приедет потом.
    """
    rule = places.save(session, user.id, merchant, title, category)
    pattern = rule.pattern
    record_action(session, user, taps=1)
    session.flush()
    reclassify(session, user.id)
    return PlaceLine(
        pattern=pattern,
        title=title.strip(),
        category=(rule.category_code or None),
        operations_count=_place_counts(session, user.id).get(pattern, 0),
    )


def delete_place(session: Session, user: User, pattern: str) -> bool:
    """Убрать название. Операции возвращаются к банковскому имени."""
    if not places.drop(session, user.id, pattern):
        return False
    session.flush()
    reclassify(session, user.id)
    return True


# --- отчёты ----------------------------------------------------------------


def report_for(session: Session, user_id: int, period: Period) -> reports.PeriodReport:
    return reports.build_report(session, user_id, period)


def refresh_streak(session: Session, user: User) -> None:
    """Вызывается при открытии сводки: засчитывает тихий день, если он тихий."""
    keep_streak_alive(session, user)


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
