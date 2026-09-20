"""Таблицы. Три слоя данных.

    RawTransaction  — сырой факт из выписки, иммутабелен.
    Operation       — экономическое событие, склеенное из 1..N сырых фактов.
    Posting         — проводка двойной записи, единственный источник аналитики.

Аналитика никогда не считает суммы по Operation: она считает их по Posting.
Поэтому недельные срезы складываются в месячный по построению, а не потому
что мы аккуратно написали SQL.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Money, TimestampMixin
from app.models.enums import (
    AccountKind,
    ClaimKind,
    ClaimStatus,
    CounterpartyKind,
    MatchType,
    OperationKind,
    OperationSource,
    OperationStatus,
    ReviewKind,
    ReviewStatus,
)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    tz: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")

    # Персональный секрет загрузки. Файлы со всех телефонов приходят на один
    # адрес, и опознавать человека по tg_id нельзя: его видит каждый, кому он
    # писал, и его можно перебрать — то есть залить операции в чужую книгу.
    # Токен случайный, из tg_id не выводится, выдаётся по запросу владельца.
    #
    # Хранится как есть, а не хешем, осознанно: бот и Mini App обязаны уметь
    # показать ссылку повторно, иначе единственным сценарием восстановления
    # был бы перевыпуск. Цена известна — дамп базы даёт рабочие ссылки; она
    # приемлема ровно потому, что в том же дампе лежат и сами данные.
    upload_token: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    # Когда выпущен. Нужно, чтобы отзыв был виден человеку: после «выпустить
    # новую» бот честно показывает дату, а не просто другую строку.
    upload_token_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Механика привычки
    streak_days: Mapped[int] = mapped_column(Integer, default=0)
    streak_last_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    best_streak: Mapped[int] = mapped_column(Integer, default=0)
    digest_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    digest_hour: Mapped[int] = mapped_column(Integer, default=21)

    accounts: Mapped[list[Account]] = relationship(back_populates="user")


class Counterparty(Base, TimestampMixin):
    """Контрагент: человек, магазин, банк или я сам.

    Отличать человека от магазина критично: входящий перевод от человека —
    кандидат в возврат долга, входящий от магазина — кандидат в возврат товара.
    """

    __tablename__ = "counterparties"
    __table_args__ = (UniqueConstraint("user_id", "norm_key", name="uq_cp_user_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[CounterpartyKind] = mapped_column(String(16), default=CounterpartyKind.UNKNOWN)
    display_name: Mapped[str] = mapped_column(String(200))
    norm_key: Mapped[str] = mapped_column(String(200), index=True)
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    default_category: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Account(Base, TimestampMixin):
    """Счёт двойной записи: и реальная карта, и виртуальная дебиторка Ивана."""

    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_account_user_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[AccountKind] = mapped_column(String(16), index=True)
    # Стабильный ключ для сопоставления выписок: tbank:1234, expense:food
    key: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(200))
    bank: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last4: Mapped[str | None] = mapped_column(String(8), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    # Для EXPENSE/INCOME: код категории. Для RECEIVABLE/PAYABLE: контрагент.
    category_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    counterparty_id: Mapped[int | None] = mapped_column(
        ForeignKey("counterparties.id"), nullable=True
    )
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship(back_populates="accounts")
    counterparty: Mapped[Counterparty | None] = relationship()


class ImportBatch(Base, TimestampMixin):
    """Одна загрузка. Нужна, чтобы импорт можно было откатить целиком.

    И чтобы на вопрос «что именно я скинул» был ответ: батч — единственное
    место, где встречаются файл на диске и операции, которые из него
    получились. Обратный путь идёт через RawTransaction.batch_id, который
    переживает пересчёт, а не через операцию: операция склеивается из
    нескольких сырых строк и в пограничном случае приезжает из двух разных
    загрузок, так что колонка «файл» в ней была бы враньём.
    """

    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    source: Mapped[str] = mapped_column(String(32))  # tbank, sber, alfa, wb, generic
    filename: Mapped[str] = mapped_column(String(255), default="")
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_new: Mapped[int] = mapped_column(Integer, default=0)
    rows_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    period_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Чем эта загрузка была на входе. Имя на диске — единственная ниточка от
    # записи в базе к файлу в data/uploads: имя генерируем мы, в ответе оно
    # уже есть (stored_as), и без него след обрывается на границе HTTP.
    # server_default стоит у всех четырёх не для красоты: база задеплоена, а
    # ALTER TABLE ADD COLUMN в SQLite без значения по умолчанию на NOT NULL
    # колонке не работает (см. app/db.py).
    stored_as: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    content_type: Mapped[str] = mapped_column(
        String(64), default="", server_default=text("''")
    )
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # Откуда пришло: shortcut, bot, miniapp, service. Ответ на «кто прислал»
    # в терминах канала, а не человека — человек уже есть в user_id.
    channel: Mapped[str] = mapped_column(String(16), default="", server_default=text("''"))

    # Хеши строк, которые в этой загрузке оказались уже известными. Строка-
    # дубль в базу не пишется (на этом держится дедупликация), поэтому без
    # этого списка повторная присылка того же скриншота не связана ни с
    # одной операцией: карточка честно пишет «8 дублей» и показывает пустой
    # список. А повтор — самый частый случай: человек фотографирует список
    # операций раз в неделю, и пересечение с прошлым разом неизбежно.
    duplicate_hashes: Mapped[list] = mapped_column(
        JSON, default=list, server_default=text("'[]'")
    )


class RawTransaction(Base, TimestampMixin):
    """Сырая строка выписки. Не редактируется никогда.

    amount: знаковые копейки с точки зрения счёта (+ приход, - расход).
    dedup_hash делает повторную загрузку той же выписки безопасной и
    склеивает одну и ту же операцию, приехавшую из двух банков.
    """

    __tablename__ = "raw_transactions"
    __table_args__ = (
        UniqueConstraint("user_id", "dedup_hash", name="uq_raw_user_hash"),
        Index("ix_raw_user_time", "user_id", "booked_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"), nullable=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)

    booked_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    amount: Mapped[Money] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    description: Mapped[str] = mapped_column(Text, default="")
    mcc: Mapped[str | None] = mapped_column(String(8), nullable=True)
    counterparty_raw: Mapped[str] = mapped_column(String(255), default="")
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dedup_hash: Mapped[str] = mapped_column(String(64), index=True)
    # Откуда факт: выписка, быстрый ввод или голосовое. Ручной ввод наличных
    # хранится здесь же, чтобы у пересчёта был ровно один вид входных данных.
    source: Mapped[OperationSource] = mapped_column(
        String(16), default=OperationSource.IMPORT
    )

    # Заполняется обогащением до классификации
    counterparty_id: Mapped[int | None] = mapped_column(
        ForeignKey("counterparties.id"), nullable=True
    )
    purpose_tags: Mapped[list] = mapped_column(JSON, default=list)  # debt, meal, ...
    mcc_category: Mapped[str | None] = mapped_column(String(64), nullable=True)

    operation_id: Mapped[int | None] = mapped_column(
        ForeignKey("operations.id"), nullable=True, index=True
    )

    account: Mapped[Account] = relationship()
    counterparty: Mapped[Counterparty | None] = relationship()
    operation: Mapped[Operation | None] = relationship(back_populates="legs")


class Operation(Base, TimestampMixin):
    """Экономическое событие. Может собираться из нескольких сырых строк.

    amount        — номинал события (сколько денег двигалось), всегда > 0.
    spend_amount  — сколько из этого является личной тратой. Для перевода
                    себе 0, для счёта за пятерых одна пятая. Денормализация
                    для UI; источник правды — проводки.
    """

    __tablename__ = "operations"
    __table_args__ = (Index("ix_op_user_time", "user_id", "occurred_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

    kind: Mapped[OperationKind] = mapped_column(String(24), index=True)
    status: Mapped[OperationStatus] = mapped_column(
        String(16), default=OperationStatus.AUTO, index=True
    )
    source: Mapped[OperationSource] = mapped_column(String(16), default=OperationSource.IMPORT)

    occurred_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    amount: Mapped[Money] = mapped_column(Integer, default=0)
    spend_amount: Mapped[Money] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="RUB")

    category_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    counterparty_id: Mapped[int | None] = mapped_column(
        ForeignKey("counterparties.id"), nullable=True
    )

    # Объяснимость: почему система решила именно так. Показывается пользователю.
    rule: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    explanation: Mapped[str] = mapped_column(Text, default="")
    # Человек объяснил операцию своими словами. Категория при этом может и не
    # найтись («вернул Диме за ремонт»), но деньги перестают быть непонятными:
    # метрика осознанности такие траты в неясные больше не записывает.
    # server_default — база задеплоена, колонка доезжает через upgrade_schema.
    explained: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0")
    )

    # use_alter: claims и operations ссылаются друг на друга, и без этого
    # SQLAlchemy не может упорядочить CREATE/DROP таблиц.
    claim_id: Mapped[int | None] = mapped_column(
        ForeignKey("claims.id", use_alter=True), nullable=True
    )
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("operations.id"), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    extra: Mapped[dict] = mapped_column(JSON, default=dict)

    legs: Mapped[list[RawTransaction]] = relationship(back_populates="operation")
    postings: Mapped[list[Posting]] = relationship(
        back_populates="operation", cascade="all, delete-orphan"
    )
    counterparty: Mapped[Counterparty | None] = relationship()
    claim: Mapped[Claim | None] = relationship(foreign_keys=[claim_id])


class Posting(Base):
    """Проводка двойной записи. Дебет положительный.

    Активы и расходы: + = увеличение.
    Обязательства и доходы: + = уменьшение (то есть доход пишется минусом).

    Инвариант: sum(amount) по одной операции == 0.

    at может отличаться от operation.occurred_at: например, невозврат доли
    по общему счёту списывается в расход датой дедлайна, а не датой ужина.
    Это и есть механизм, который держит сходимость периодов.
    """

    __tablename__ = "postings"
    __table_args__ = (
        Index("ix_posting_user_at", "user_id", "at"),
        Index("ix_posting_acct_at", "account_id", "at"),
        Index("ix_posting_cat_at", "user_id", "category_code", "at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    operation_id: Mapped[int] = mapped_column(ForeignKey("operations.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)

    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    amount: Mapped[Money] = mapped_column(Integer)
    # Денормализация: вид счёта и категория, чтобы аналитика не делала JOIN.
    account_kind: Mapped[AccountKind] = mapped_column(String(16), index=True)
    category_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    memo: Mapped[str] = mapped_column(String(255), default="")

    operation: Mapped[Operation] = relationship(back_populates="postings")
    account: Mapped[Account] = relationship()


class Claim(Base, TimestampMixin):
    """Обязательство: выданный/взятый долг или доля компании по общему счёту.

    Один механизм на долги и на общие счета: и там, и там есть ожидаемые
    поступления, которые нельзя считать доходом, и есть срок, после которого
    невозвращённое становится тратой.
    """

    __tablename__ = "claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[ClaimKind] = mapped_column(String(20), index=True)
    status: Mapped[ClaimStatus] = mapped_column(String(16), default=ClaimStatus.OPEN, index=True)

    counterparty_id: Mapped[int | None] = mapped_column(
        ForeignKey("counterparties.id"), nullable=True
    )
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))  # виртуальный счёт

    principal: Mapped[Money] = mapped_column(Integer)  # сколько ожидается обратно
    settled_amount: Mapped[Money] = mapped_column(Integer, default=0)

    # Только для SHARED_BILL
    total_bill: Mapped[Money | None] = mapped_column(Integer, nullable=True)
    participants: Mapped[int | None] = mapped_column(Integer, nullable=True)
    my_share: Mapped[Money | None] = mapped_column(Integer, nullable=True)

    opened_at: Mapped[datetime] = mapped_column(DateTime)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source_operation_id: Mapped[int | None] = mapped_column(
        ForeignKey("operations.id", use_alter=True), nullable=True
    )
    label: Mapped[str] = mapped_column(String(200), default="")

    counterparty: Mapped[Counterparty | None] = relationship()
    account: Mapped[Account] = relationship()

    @property
    def outstanding(self) -> Money:
        return max(0, self.principal - self.settled_amount)


class ClaimSettlement(Base):
    """Факт частичного или полного закрытия обязательства."""

    __tablename__ = "claim_settlements"

    id: Mapped[int] = mapped_column(primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id"), index=True)
    operation_id: Mapped[int] = mapped_column(ForeignKey("operations.id"))
    amount: Mapped[Money] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(DateTime)


class ReviewItem(Base, TimestampMixin):
    """Вопрос пользователю, закрываемый одним тапом.

    Это и есть механика регулярного использования: инструмент сам приходит
    с конкретным вопросом, а не просит вести учёт.
    """

    __tablename__ = "review_items"
    # sqlite_autoincrement: при пересчёте старые вопросы удаляются, и без
    # этого SQLite переиспользует освободившиеся id. Клиент, который держит
    # id вопроса на экране, ответил бы после пересчёта на другой вопрос,
    # получивший тот же номер. Ошибка тихая и крайне неприятная.
    __table_args__ = (
        Index("ix_review_user_status", "user_id", "status"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    operation_id: Mapped[int | None] = mapped_column(ForeignKey("operations.id"), nullable=True)
    # Операции пересоздаются при каждом пересчёте, поэтому вопрос привязан не
    # к операции, а к устойчивому якорю — хешу сырой транзакции.
    anchor_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    kind: Mapped[ReviewKind] = mapped_column(String(32), index=True)
    status: Mapped[ReviewStatus] = mapped_column(String(16), default=ReviewStatus.OPEN)

    question: Mapped[str] = mapped_column(Text)
    options: Mapped[list] = mapped_column(JSON, default=list)  # [{code, label, payload}]
    answer_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    answer_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # Приоритет = влияние на цифры. Сортируем очередь по нему: сначала то,
    # что сильнее искажает аналитику.
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    operation: Mapped[Operation | None] = relationship()


class Decision(Base, TimestampMixin):
    """Решение пользователя об операции. Переживает пересчёт.

    Классификация — чистая функция от (сырые транзакции, решения). Операции
    и проводки пересобираются с нуля при каждом пересчёте, а вот это —
    единственное, что пользователь сказал сам, и оно неприкосновенно.

    anchor_hash — dedup_hash сырой транзакции: устойчивый ключ, не зависящий
    от номеров строк в базе.
    """

    __tablename__ = "decisions"
    __table_args__ = (
        UniqueConstraint("user_id", "anchor_hash", "kind", name="uq_decision_anchor_kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    anchor_hash: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class MerchantRule(Base, TimestampMixin):
    """Правило категоризации. user_id = NULL — глобальное правило-словарь.

    Здесь же живут «мои места»: правила с match_type=CONTAINS и непустым
    title. Отдельной таблицы у них нет осознанно — «продавец -> что это
    значит» уже описано здесь, и вторая таблица завела бы второй ключ, вторую
    нормализацию и второе место, где их надо держать в согласии. Подробности
    и правила сопоставления — в app/classify/places.py.
    """

    __tablename__ = "merchant_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    match_type: Mapped[MatchType] = mapped_column(String(16))
    pattern: Mapped[str] = mapped_column(String(255), index=True)
    # Пусто — правило только про категорию (так их пишет языковая модель).
    # Непусто — как человек называет это место сам; название встаёт на все
    # его траты в этом месте, и на прошлые тоже.
    # server_default — база задеплоена, колонка доезжает через upgrade_schema.
    title: Mapped[str] = mapped_column(String(200), default="", server_default=text("''"))
    # Для «моего места» пустая строка значит «категорию не трогаем». NULL был
    # бы честнее, но колонка в задеплоенной базе объявлена NOT NULL, а в
    # SQLite это меняется только пересборкой таблицы.
    category_code: Mapped[str] = mapped_column(String(64))
    counterparty_kind: Mapped[CounterpartyKind | None] = mapped_column(String(16), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)


class Subscription(Base, TimestampMixin):
    """Обнаруженное регулярное списание."""

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    counterparty_id: Mapped[int | None] = mapped_column(
        ForeignKey("counterparties.id"), nullable=True
    )
    label: Mapped[str] = mapped_column(String(200))
    amount: Mapped[Money] = mapped_column(Integer)
    period_days: Mapped[int] = mapped_column(Integer, default=30)
    occurrences: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = mapped_column(DateTime)
    last_seen: Mapped[datetime] = mapped_column(DateTime)
    category_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)


class DailyActivity(Base):
    """Сколько действий пользователь сделал в конкретный день.

    Нужно, чтобы на защите назвать реальное число действий в день, а не
    придуманное: считаем по факту.
    """

    __tablename__ = "daily_activity"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_activity_user_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    taps: Mapped[int] = mapped_column(Integer, default=0)  # ответы на вопросы
    manual_entries: Mapped[int] = mapped_column(Integer, default=0)
    imports: Mapped[int] = mapped_column(Integer, default=0)
