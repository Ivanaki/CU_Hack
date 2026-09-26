"""Эндпоинты. Две группы с разной аутентификацией.

/api/v1/me/*      — Mini App, подпись Telegram, только свои данные.
/api/v1/ingest/*  — служебная заливка по ключу, можно от имени любого
                    пользователя. Именно сюда внешний сервис шлёт скриншоты.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.analytics import insights as insights_mod
from app.analytics import reports as reports_mod
from app.analytics.periods import GRANULARITY_TITLE, WEEK, Period, split_into_weeks, year
from app.api import schemas as s
from app.api.analytics import PERIOD_HELP, resolve_period
from app.api.analytics import analytics as analytics_router
from app.api.auth import current_user, db_session, require_service_key
from app.api.uploads import _STORED_NAME, file_url, private_dir, public_base, store_image
from app.classify import categories as cats
from app.classify.phone_identities import PhoneRejected, delete_identity, save_identity
from app.classify.pipeline import reclassify
from app.classify.places import PlaceRejected
from app.config import Settings, get_settings
from app.ledger.invariants import run_all
from app.models import (
    Account,
    Claim,
    ClaimSettlement,
    Counterparty,
    DailyActivity,
    Decision,
    ImportBatch,
    MerchantRule,
    Operation,
    OperationSource,
    PhoneIdentity,
    Posting,
    RawTransaction,
    ReviewItem,
    Subscription,
    User,
)
from app.service import (
    AnswerRejected,
    CategoryChangeRejected,
    add_quick_expense,
    answer_question,
    average_actions_per_day,
    batch_operations,
    batch_questions,
    batches_by_operation,
    change_expense_category,
    count_open_questions,
    delete_place,
    get_or_create_user,
    ingest_screenshot,
    ingest_statement,
    issue_upload_token,
    latest_data_month,
    list_places,
    open_questions,
    recent_imports,
    refresh_streak,
    report_for,
    rotate_upload_token,
    save_place,
    skip_question,
    user_batch,
)

log = logging.getLogger(__name__)

me = APIRouter(prefix="/api/v1/me", tags=["Mini App"])
# Разрезы вкладки «Отчёты» вынесены в отдельный модуль, но адресом остаются
# в /api/v1/me и едут вместе с ним: отдельного подключения в app.py не нужно,
# иначе тот же путь зарегистрировался бы дважды.
me.include_router(analytics_router)
ingest = APIRouter(
    prefix="/api/v1/ingest", tags=["Служебная заливка"], dependencies=[Depends(require_service_key)]
)
public = APIRouter(tags=["Служебное"])

MAX_UPLOAD = 12 * 1024 * 1024


def _resolve_period(session: Session, user_id: int, spec: str | None) -> Period:
    """Период из строки. Разбор общий с /me/analytics — он там и живёт."""
    return resolve_period(session, user_id, spec)


def _pending_operation_ids(session: Session, user_id: int, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    return set(session.scalars(
        select(RawTransaction.operation_id).where(
            RawTransaction.user_id == user_id,
            RawTransaction.is_pending.is_(True),
            RawTransaction.operation_id.in_(ids),
        )
    ).all())


# --------------------------------------------------------------------------
# Mini App
# --------------------------------------------------------------------------


@me.get("/summary", response_model=s.SummaryOut, summary="Всё для главного экрана")
def summary(
    period: str | None = Query(default=None, description=PERIOD_HELP),
    with_insights: bool = Query(default=True),
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> s.SummaryOut:
    window = _resolve_period(session, user.id, period)
    # Тихий день засчитываем в серию при открытии сводки: если система
    # ни о чём не спросила, требовать от человека действий не за что.
    refresh_streak(session, user)
    report = report_for(session, user.id, window)

    items = insights_mod.collect(session, user.id, report) if with_insights else []
    questions = open_questions(session, user.id, limit=10)
    # Срезы подходящей длины: неделя режется днями, месяц — неделями, год —
    # месяцами. Для месяца срезы и есть недели, второй раз их не считаем.
    granularity, parts = reports_mod.slice_breakdown(session, user.id, window)
    if granularity == WEEK:
        weekly = parts
    else:
        weekly = [
            (chunk, reports_mod.spend_total(session, user.id, chunk))
            for chunk in split_into_weeks(window)
        ]
    weeks = [
        s.WeekSliceOut(label=chunk.label, start=chunk.start, amount=s.MoneyOut.of(amount))
        for chunk, amount in weekly
    ]
    pct = None
    if report.prev_spend_total:
        pct = round(report.spend_delta / report.prev_spend_total * 100, 1)

    return s.SummaryOut(
        period=s.PeriodOut(
            label=window.label, start=window.start, end=window.end, days=window.days
        ),
        prev_period=s.period_out(window.shifted_back()),
        spend=s.MoneyOut.of(report.spend_total),
        income=s.MoneyOut.of(report.income_total),
        net=s.MoneyOut.of(report.net, sign=True),
        avg_per_day=s.MoneyOut.of(report.avg_per_day),
        prev_spend=s.MoneyOut.of(report.prev_spend_total),
        spend_delta=s.MoneyOut.of(report.spend_delta, sign=True),
        spend_delta_pct=pct,
        awareness=s.awareness_out(report),
        categories=[s.category_out(line) for line in report.categories],
        essential=s.MoneyOut.of(report.essential_total),
        optional=s.MoneyOut.of(report.optional_total),
        weeks=weeks,
        granularity=granularity,
        granularity_title=GRANULARITY_TITLE[granularity],
        slices=[s.slice_out(chunk, amount) for chunk, amount in parts],
        questions=[s.QuestionOut.of(item, session) for item in questions],
        open_questions=count_open_questions(session, user.id),
        insights=[s.InsightOut.of(item) for item in items],
        streak=s.StreakOut(
            days=user.streak_days,
            best=user.best_streak,
            actions_per_day=round(average_actions_per_day(session, user.id), 2),
        ),
        forecast=s.forecast_out(report),
        wallets=[
            s.wallet_out(t) for t in reports_mod.wallet_traces(session, user.id, window)
        ],
        balances={
            name: s.MoneyOut.of(value) for name, value in sorted(report.balances.items())
        },
        debts_owed_to_me=s.MoneyOut.of(report.debts_owed_to_me),
        debts_i_owe=s.MoneyOut.of(report.debts_i_owe),
        operations_count=report.operations_count,
    )


@me.delete("/data", summary="Удалить все мои данные")
def delete_my_data(
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Удаляет личную книгу, профильные настройки и принадлежащие ей файлы."""
    user_id = user.id
    operation_ids = select(Operation.id).where(Operation.user_id == user_id)
    claim_ids = select(Claim.id).where(Claim.user_id == user_id)
    stored_names = set(session.scalars(
        select(ImportBatch.stored_as).where(
            ImportBatch.user_id == user_id, ImportBatch.stored_as != ""
        )
    ).all())

    # Операции и обязательства ссылаются друг на друга. Сначала убираем
    # зависимые строки, затем размыкаем цикл и удаляем сами записи.
    session.execute(delete(ClaimSettlement).where(
        ClaimSettlement.claim_id.in_(claim_ids)
        | ClaimSettlement.operation_id.in_(operation_ids)
    ))
    session.execute(delete(Posting).where(Posting.user_id == user_id))
    session.execute(delete(ReviewItem).where(ReviewItem.user_id == user_id))
    session.execute(delete(Subscription).where(Subscription.user_id == user_id))
    session.execute(delete(DailyActivity).where(DailyActivity.user_id == user_id))
    session.execute(delete(Decision).where(Decision.user_id == user_id))
    session.execute(delete(MerchantRule).where(MerchantRule.user_id == user_id))
    session.execute(delete(PhoneIdentity).where(PhoneIdentity.user_id == user_id))
    session.execute(update(Operation).where(Operation.user_id == user_id).values(
        claim_id=None, parent_id=None
    ))
    session.execute(update(Claim).where(Claim.user_id == user_id).values(
        source_operation_id=None
    ))
    session.execute(delete(RawTransaction).where(RawTransaction.user_id == user_id))
    session.execute(delete(Claim).where(Claim.user_id == user_id))
    session.execute(delete(Operation).where(Operation.user_id == user_id))
    session.execute(delete(ImportBatch).where(ImportBatch.user_id == user_id))
    session.execute(update(Account).where(Account.user_id == user_id).values(
        counterparty_id=None
    ))
    session.execute(delete(Counterparty).where(Counterparty.user_id == user_id))
    session.execute(delete(Account).where(Account.user_id == user_id))
    session.execute(delete(User).where(User.id == user_id))
    session.flush()

    # Имена создаются сервером и сверяются со строгим шаблоном, прежде чем
    # строить путь: запись в базе не должна указывать за пределы каталога.
    private_files = [
        private_dir(settings) / name
        for name in stored_names
        if _STORED_NAME.fullmatch(name)
    ]
    for path in private_files:
        path.unlink(missing_ok=True)

    # Фиксируем полное удаление сейчас, до ответа клиенту. Авторизация при
    # следующем запросе создаст пустой аккаунт из Telegram initData.
    session.commit()
    return {"ok": True}


@me.get("/phone-identities", response_model=list[s.PhoneIdentityOut], summary="Мои номера и люди")
def phone_identities_list(
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> list[s.PhoneIdentityOut]:
    lines = session.scalars(select(PhoneIdentity).where(
        PhoneIdentity.user_id == user.id
    ).order_by(PhoneIdentity.kind, PhoneIdentity.phone)).all()
    return [s.PhoneIdentityOut(phone=line.phone, kind=line.kind, name=line.name) for line in lines]


@me.post("/phone-identities", response_model=s.PhoneIdentityOut, summary="Запомнить номер")
def phone_identity_save(
    payload: s.PhoneIdentityIn,
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> s.PhoneIdentityOut:
    try:
        line = save_identity(session, user.id, payload.phone, payload.kind, payload.name)
    except PhoneRejected as rejected:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(rejected)) from rejected
    reclassify(session, user.id)
    return s.PhoneIdentityOut(phone=line.phone, kind=line.kind, name=line.name)


@me.delete("/phone-identities/{phone}", summary="Забыть номер")
def phone_identity_delete(
    phone: str,
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> dict:
    try:
        removed = delete_identity(session, user.id, phone)
    except PhoneRejected as rejected:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(rejected)) from rejected
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "номер не найден")
    reclassify(session, user.id)
    return {"ok": True}


@me.get("/questions", response_model=list[s.QuestionOut], summary="Очередь спорных операций")
def questions(
    limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> list[s.QuestionOut]:
    items = open_questions(session, user.id, limit=limit)
    return [s.QuestionOut.of(item, session) for item in items]


@me.post("/questions/{review_id}/answer", summary="Ответить одним тапом")
def answer(
    review_id: int,
    payload: s.AnswerIn,
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> dict:
    try:
        item = answer_question(session, user, review_id, payload.code, text=payload.text or "")
    except AnswerRejected as rejected:
        # 400, а не 404: вопрос жив и ждёт нормального ответа.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(rejected)) from rejected
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "вопрос не найден или уже закрыт")
    window = latest_data_month(session, user.id)
    report = report_for(session, user.id, window)
    return {
        "ok": True,
        "awareness_percent": report.awareness.percent,
        "open_questions": count_open_questions(session, user.id),
        "streak_days": user.streak_days,
    }


@me.post("/questions/{review_id}/skip", summary="Отложить вопрос")
def skip(
    review_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> dict:
    skip_question(session, user, review_id)
    return {"ok": True, "open_questions": count_open_questions(session, user.id)}


@me.get("/operations", response_model=list[s.OperationOut], summary="Операции за период")
def operations(
    request: Request,
    period: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    only_spending: bool = Query(default=False),
    with_sources: bool = Query(
        default=False, description="Показать, из какого файла приехала каждая операция"
    ),
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> list[s.OperationOut]:
    window = _resolve_period(session, user.id, period)
    query = (
        select(Operation)
        .where(
            Operation.user_id == user.id,
            Operation.occurred_at >= window.start,
            Operation.occurred_at < window.end,
        )
        .order_by(Operation.occurred_at.desc())
        .limit(limit)
    )
    if only_spending:
        query = query.where(Operation.spend_amount != 0)
    found = list(session.scalars(query).all())
    pending_ids = _pending_operation_ids(session, user.id, [op.id for op in found])
    if not with_sources:
        return [s.OperationOut.of(op, is_pending=op.id in pending_ids) for op in found]

    # Один запрос на весь список, а не по запросу на операцию: след не
    # должен стоить N+1 похода в базу.
    sources = batches_by_operation(session, user.id, [op.id for op in found])
    return [
        s.OperationOut.of(
            op, sources=_source_refs(request, settings, sources.get(op.id, [])),
            is_pending=op.id in pending_ids,
        )
        for op in found
    ]


@me.get("/categories", response_model=list[s.CategoryChoiceOut], summary="Категории трат")
def expense_categories(
    user: User = Depends(current_user),
) -> list[s.CategoryChoiceOut]:
    return [
        s.CategoryChoiceOut(code=code, title=cats.title(code))
        for code in cats.EXPENSE_CODES if code != cats.CASH_UNALLOCATED
    ]


@me.post("/operations/{operation_id}/category", summary="Изменить категорию траты")
def operation_category(
    operation_id: int,
    payload: s.CategoryChangeIn,
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> dict:
    try:
        applied = change_expense_category(
            session, user, operation_id, payload.category, payload.scope
        )
    except CategoryChangeRejected as rejected:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(rejected)) from rejected
    if applied is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "операция не найдена")
    return {"ok": True, "applied": applied}


@me.get("/insights", response_model=list[s.InsightOut], summary="Советы по итогам периода")
def advice(
    period: str | None = Query(default=None),
    polish: bool = Query(default=False, description="переписать формулировки языковой моделью"),
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> list[s.InsightOut]:
    window = _resolve_period(session, user.id, period)
    report = report_for(session, user.id, window)
    items = insights_mod.collect(session, user.id, report)
    if polish:
        items = insights_mod.polish(items)
    return [s.InsightOut.of(item) for item in items]


@me.post("/expenses", response_model=s.IngestResultOut, summary="Быстрый ввод траты наличными")
def quick_expense(
    payload: s.QuickExpenseIn,
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> s.IngestResultOut:
    row, outcome = add_quick_expense(
        session, user, payload.text, when=payload.occurred_at, channel="miniapp"
    )
    if row is None:
        return s.IngestResultOut(ok=False, error=outcome.error)
    return _ingest_out(outcome)


# --------------------------------------------------------------------------
# Мои места: человек называет продавца по-своему
# --------------------------------------------------------------------------


@me.get("/places", response_model=list[s.PlaceOut], summary="Мои названия мест")
def places_list(
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> list[s.PlaceOut]:
    return [s.PlaceOut.of(line) for line in list_places(session, user.id)]


@me.post("/places", summary="Назвать место по-своему")
def places_save(
    payload: s.PlaceIn,
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> dict:
    """Создаёт или обновляет правило и пересчитывает книгу.

    `applied` — сколько операций сейчас носят это имя: и будущие, и те, что
    уже были загружены. Правило меняет название и категорию, но не деньги.
    """
    try:
        place = save_place(
            session, user, payload.merchant, payload.title, payload.category
        )
    except PlaceRejected as rejected:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(rejected)) from rejected
    return {"ok": True, "applied": place.operations_count, "place": s.PlaceOut.of(place)}


@me.delete("/places/{pattern:path}", summary="Убрать название места")
def places_delete(
    pattern: str,
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> dict:
    """Операции возвращаются к тому имени, которое даёт банк.

    Чужое правило отсюда не видно: ищем только среди своих, поэтому попытка
    удалить не своё неотличима от попытки удалить несуществующее.
    """
    if not delete_place(session, user, pattern):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "такого места у вас нет")
    return {"ok": True, "applied": 0}


@me.post(
    "/statements",
    response_model=s.IngestResultOut,
    summary="Загрузить свою выписку из Mini App",
)
async def upload_statement(
    files: list[UploadFile] | None = File(
        default=None, description="Одна или несколько CSV/PDF-выписок банка (до 10 файлов)"
    ),
    file: UploadFile | None = File(
        default=None, description="Одна CSV/PDF-выписка банка (совместимость со старым клиентом)"
    ),
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> s.IngestResultOut:
    """То же, что прислать файл(ы) боту, но не выходя из приложения.

    Без этого Mini App умел бы только показывать, а класть данные пришлось бы
    возвращаться в чат — лишний шаг там, где мы обещаем минимум действий.
    """
    uploads = files or ([file] if file is not None else [])
    if not uploads:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "пустая загрузка")
    if len(uploads) > 10:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "за раз можно загрузить не больше 10 выписок",
        )

    payloads: list[tuple[bytes, str]] = []
    for item in uploads:
        data = await item.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"{item.filename or 'Файл'} больше 12 МБ",
            )
        if not data and len(uploads) == 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "пустой файл")
        payloads.append((data, item.filename or "statement.csv"))

    user_id = user.id

    def work():
        owner = session.get(User, user_id)
        if len(payloads) == 1:
            data, name = payloads[0]
            return _ingest_out(
                ingest_statement(session, owner, data, name, channel="miniapp")
            )

        per_file: list[s.StatementFileOut] = []
        successes: list[s.IngestResultOut] = []
        for data, name in payloads:
            outcome = ingest_statement(session, owner, data, name, channel="miniapp")
            if not outcome.ok:
                per_file.append(
                    s.StatementFileOut(filename=name, ok=False, error=outcome.error)
                )
                continue
            result = _ingest_out(outcome)
            successes.append(result)
            per_file.append(
                s.StatementFileOut(
                    filename=name,
                    ok=True,
                    source=result.source,
                    added=result.added,
                    duplicates=result.duplicates,
                    superseded=result.superseded,
                    extraction_method=result.extraction_method,
                    pending_count=result.pending_count,
                )
            )

        if not successes:
            details = "; ".join(f"{item.filename}: {item.error}" for item in per_file)
            return s.IngestResultOut(ok=False, error=details, files=per_file)

        latest = successes[-1]
        methods = {item.extraction_method for item in successes}
        method = next(iter(methods)) if len(methods) == 1 else "mixed"
        return s.IngestResultOut(
            ok=True,
            source="Выписки",
            added=sum(item.added for item in successes),
            duplicates=sum(item.duplicates for item in successes),
            superseded=sum(item.superseded for item in successes),
            operations=latest.operations,
            questions=latest.questions,
            unresolved=latest.unresolved,
            extraction_method=method,
            pending_count=sum(item.pending_count for item in successes),
            batch_id=latest.batch_id,
            files=per_file,
        )

    # Разбор выписки — это ещё и полный пересчёт книги, секунды процессора.
    # В корутине он встал бы поперёк и API, и бота, который живёт в том же
    # процессе: на демонстрации это выглядит как «залил файл — всё упало».
    return await asyncio.to_thread(work)


@me.post(
    "/images",
    response_model=s.IngestResultOut,
    summary="Загрузить скриншот банка или фото чека из Mini App",
)
async def upload_images(
    files: list[UploadFile] = File(..., description="Скриншоты или фотографии чеков"),
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> s.IngestResultOut:
    blobs: list[bytes] = []
    shown_name = ""
    for item in files[:10]:
        data = await item.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"{item.filename} больше 12 МБ"
            )
        if data:
            blobs.append(data)
            shown_name = shown_name or (item.filename or "")
    if not blobs:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "пустая загрузка")

    # Картинку сохраняем так же, как это делает /upload: иначе в «Что я
    # присылал» у Mini App пустая карточка без файла, и посмотреть, что
    # именно ты прислал, не на что. Имя на диске одно на загрузку, поэтому
    # при нескольких картинках в след попадает первая.
    stored_as, content_type = store_image(settings, blobs[0])
    user_id = user.id

    def work():
        owner = session.get(User, user_id)
        return ingest_screenshot(
            session, owner, blobs, channel="miniapp",
            stored_as=stored_as, content_type=content_type,
            # Имя из запроса — единственное, чем две присылки отличаются
            # в списке: без него обе называются «statement».
            filename=shown_name,
        )

    # Разбор картинки ходит в сеть и считает пересчёт: в корутине он
    # останавливает и API, и бота в том же процессе (см. upload_statement).
    outcome = await asyncio.to_thread(work)
    result = _ingest_out(outcome)
    result.source = {
        "receipt": "Чек",
        "statement": "Скриншот",
    }.get(outcome.image_kind, result.source or "Картинка")
    return result


# --------------------------------------------------------------------------
# Персональная ссылка для загрузки и след загруженного файла
# --------------------------------------------------------------------------


def _upload_link_out(
    request: Request, settings: Settings, user: User, token: str
) -> s.UploadLinkOut:
    """Собирает ответ про ссылку.

    Адрес общий для всех и токена не содержит: секрет едет заголовком,
    потому что полный URL со строкой запроса пишут все прокси подряд, а
    ссылкой человек может поделиться целиком, не заметив, что отдал доступ.
    """
    base = public_base(settings, request)
    return s.UploadLinkOut(
        url=f"{base}/upload",
        token=token,
        curl=(
            f'curl -X POST "{base}/upload" '
            f'-H "X-Upload-Token: {token}" '
            f'-H "Content-Type: image/png" --data-binary @shot.png'
        ),
        issued_at=user.upload_token_at,
    )


@me.get(
    "/upload-link",
    response_model=s.UploadLinkOut,
    summary="Личная ссылка для отправки файлов с телефона",
)
def upload_link(
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> s.UploadLinkOut:
    """Токен создаётся при первом обращении, дальше отдаётся тот же."""
    return _upload_link_out(request, settings, user, issue_upload_token(session, user))


@me.post(
    "/upload-link/rotate",
    response_model=s.UploadLinkOut,
    summary="Выпустить новую ссылку, старую отозвать",
)
def upload_link_rotate(
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> s.UploadLinkOut:
    """Если ссылку засветили — эта ручка немедленно ломает старую."""
    return _upload_link_out(request, settings, user, rotate_upload_token(session, user))


def _own_file_url(request: Request, settings: Settings, batch: ImportBatch) -> str | None:
    """Ссылка на свой файл — с подписью.

    Личный файл лежит в закрытом каталоге и по голому адресу не
    отдаётся. Подпись в ссылке — единственный способ показать превью в
    Mini App: тег <img> заголовков не отправляет.
    """
    if not batch.stored_as:
        return None
    return file_url(settings, request, batch.stored_as, signed=True)


def _source_refs(
    request: Request, settings: Settings, batches: list[ImportBatch]
) -> list[s.SourceRefOut]:
    return [
        s.SourceRefOut.of(batch, file_url=_own_file_url(request, settings, batch))
        for batch in batches
    ]


def _import_out(
    request: Request, settings: Settings, session: Session, batch: ImportBatch
) -> s.ImportOut:
    return s.ImportOut.of(
        batch,
        operations=len(batch_operations(session, batch)),
        questions=len(batch_questions(session, batch)),
        file_url=_own_file_url(request, settings, batch),
    )


@me.get("/imports", response_model=list[s.ImportOut], summary="Что я присылал")
def imports(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> list[s.ImportOut]:
    return [
        _import_out(request, settings, session, batch)
        for batch in recent_imports(session, user.id, limit=limit)
    ]


@me.get(
    "/imports/{batch_id}",
    response_model=s.ImportDetailOut,
    summary="Что получилось из одной загрузки",
)
def import_detail(
    batch_id: int,
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> s.ImportDetailOut:
    batch = user_batch(session, user.id, batch_id)
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "загрузка не найдена")
    found = batch_operations(session, batch)
    pending_ids = _pending_operation_ids(session, user.id, [op.id for op in found])
    # След заполняем и здесь, одним запросом на весь список: операция,
    # склеенная из двух присылок, обязана выглядеть одинаково и в карточке
    # загрузки, и в списке операций — иначе клиент рисует её по-разному в
    # двух местах, а вторая половина её источников не видна вовсе.
    sources = batches_by_operation(session, user.id, [op.id for op in found])
    return s.ImportDetailOut(
        batch=_import_out(request, settings, session, batch),
        operations=[
            s.OperationOut.of(
                op, sources=_source_refs(request, settings, sources.get(op.id, [])),
                is_pending=op.id in pending_ids,
            )
            for op in found
        ],
        questions=[s.QuestionOut.of(item, session) for item in batch_questions(session, batch)],
    )


@me.get("/checkup", summary="Сходятся ли цифры")
def checkup(
    period: str | None = Query(default=None, description=PERIOD_HELP),
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> dict:
    """Предъявляет сходимость на всех уровнях, а не только недели в месяц.

    Кейс снимает баллы за «суммы недельных срезов не складываются в
    месячный», и одна проверка закрывала ровно эту формулировку. Проверок
    четыре уровня: дни и недели в месяц, недели и месяцы в год, плюс
    категории, продавцы и дни недели — каждая часть считается отдельным
    запросом к проводкам.
    """
    window = _resolve_period(session, user.id, period)
    total = reports_mod.spend_total(session, user.id, window)
    checks = reports_mod.additivity_checks(session, user.id, window, with_days=True)

    # Год вокруг того же месяца: месяцы в год — тот уровень, который в
    # тестах есть, а в продукте до сих пор не показывался.
    around = year(window.start.year)
    if not window.is_calendar_year:
        checks += reports_mod.additivity_checks(session, user.id, around, with_days=False)

    by_weeks = next(
        (check.parts for check in checks if check.parts_name == "недели"), total
    )
    result = run_all(session, user.id)
    return {
        "ok": result.ok and all(check.ok for check in checks),
        # Поля старого ответа: на них смотрит уже собранный клиент.
        "weeks_sum_matches_month": by_weeks == total,
        "month_total_kopecks": total,
        "weeks_total_kopecks": by_weeks,
        "period": window.label,
        "levels": [
            {
                "parts": check.parts_name,
                "title": check.title,
                "unit": check.unit,
                "total_kopecks": check.total,
                "parts_kopecks": check.parts,
                "parts_count": check.parts_count,
                "ok": check.ok,
            }
            for check in checks
        ],
        "problems": result.problems,
    }


# --------------------------------------------------------------------------
# Служебная заливка
# --------------------------------------------------------------------------


def _ingest_out(outcome) -> s.IngestResultOut:  # noqa: ANN001 — service.IngestOutcome
    if not outcome.ok:
        return s.IngestResultOut(ok=False, error=outcome.error)
    batch = outcome.imported.batch
    return s.IngestResultOut(
        ok=True,
        source=batch.source,
        added=len(outcome.imported.new_rows),
        duplicates=outcome.imported.duplicates,
        superseded=outcome.imported.superseded,
        operations=outcome.operations,
        questions=outcome.questions,
        unresolved=s.MoneyOut.of(outcome.unresolved),
        batch_id=batch.id,
        extraction_method=outcome.imported.extraction_method,
        pending_count=outcome.imported.pending_count,
    )


def _target_user(session: Session, tg_id: int, name: str = "") -> User:
    return get_or_create_user(session, tg_id, name)


@ingest.post(
    "/screenshot",
    response_model=s.IngestResultOut,
    summary="Залить скриншот(ы) приложения банка за пользователя",
)
async def ingest_screenshot_endpoint(
    tg_id: int = Form(..., description="Telegram id пользователя, за которого заливаем"),
    files: list[UploadFile] = File(..., description="Скриншоты списка операций"),
    display_name: str = Form(default=""),
    session: Session = Depends(db_session),
) -> s.IngestResultOut:
    """Приём скриншотов от внешнего сервиса.

    Пользователь заводится по tg_id при первом обращении, так что заливать
    можно ещё до того, как человек напишет боту: когда он придёт, данные
    уже будут на месте.
    """
    blobs: list[bytes] = []
    for item in files[:10]:
        data = await item.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"{item.filename} велик")
        if data:
            blobs.append(data)
    if not blobs:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "пустая загрузка")

    user_id = _target_user(session, tg_id, display_name).id

    def work():
        owner = session.get(User, user_id)
        return ingest_screenshot(session, owner, blobs, channel="service")

    # Разбор картинки — шесть секунд сети и счёта. Оставить его в корутине
    # значит на эти шесть секунд остановить весь процесс: и остальные
    # запросы API, и бота, который живёт здесь же (за туннелем это 502).
    outcome = await asyncio.to_thread(work)
    log.info("заливка скриншотов для %s: %s", tg_id, outcome.error or "ок")
    return _ingest_out(outcome)


@ingest.post(
    "/statement",
    response_model=s.IngestResultOut,
    summary="Залить CSV или PDF-выписку за пользователя",
)
async def ingest_statement_endpoint(
    tg_id: int = Form(...),
    file: UploadFile = File(...),
    display_name: str = Form(default=""),
    session: Session = Depends(db_session),
) -> s.IngestResultOut:
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "файл велик")
    user = _target_user(session, tg_id, display_name)
    outcome = ingest_statement(
        session, user, data, file.filename or "statement.csv", channel="service"
    )
    return _ingest_out(outcome)


@ingest.post(
    "/expense", response_model=s.IngestResultOut, summary="Залить трату текстом за пользователя"
)
def ingest_expense_endpoint(
    tg_id: int = Body(..., embed=True),
    text: str = Body(..., embed=True),
    occurred_at: datetime | None = Body(default=None, embed=True),
    session: Session = Depends(db_session),
) -> s.IngestResultOut:
    user = _target_user(session, tg_id)
    row, outcome = add_quick_expense(
        session, user, text, source=OperationSource.MANUAL, when=occurred_at,
        channel="service",
    )
    if row is None:
        return s.IngestResultOut(ok=False, error=outcome.error)
    return _ingest_out(outcome)


@ingest.get("/users", summary="Кто уже заведён")
def list_users(session: Session = Depends(db_session)) -> list[dict]:
    users = session.scalars(select(User).order_by(User.id)).all()
    return [
        {"tg_id": u.tg_id, "name": u.display_name, "streak": u.streak_days} for u in users
    ]


# --------------------------------------------------------------------------
# Служебное
# --------------------------------------------------------------------------


@public.get("/healthz", summary="Живость")
def healthz() -> dict:
    from app.config import get_settings

    settings = get_settings()
    return {
        "ok": True,
        "llm": settings.llm_provider,
        "vision_model": settings.llm_model_vision,
        "ingest_api": bool(settings.ingest_api_key),
    }
