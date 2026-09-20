"""Эндпоинты. Две группы с разной аутентификацией.

/api/v1/me/*      — Mini App, подпись Telegram, только свои данные.
/api/v1/ingest/*  — служебная заливка по ключу, можно от имени любого
                    пользователя. Именно сюда внешний сервис шлёт скриншоты.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics import insights as insights_mod
from app.analytics import reports as reports_mod
from app.analytics.periods import Period, day, month, parse_period, split_into_weeks, week, year
from app.api import schemas as s
from app.api.auth import current_user, db_session, require_service_key
from app.ledger.invariants import run_all
from app.models import Operation, OperationSource, User
from app.service import (
    add_quick_expense,
    answer_question,
    average_actions_per_day,
    count_open_questions,
    get_or_create_user,
    ingest_screenshot,
    ingest_statement,
    latest_data_month,
    open_questions,
    report_for,
    skip_question,
)

log = logging.getLogger(__name__)

me = APIRouter(prefix="/api/v1/me", tags=["Mini App"])
ingest = APIRouter(
    prefix="/api/v1/ingest", tags=["Служебная заливка"], dependencies=[Depends(require_service_key)]
)
public = APIRouter(tags=["Служебное"])

MAX_UPLOAD = 12 * 1024 * 1024


def _resolve_period(session: Session, user_id: int, spec: str | None) -> Period:
    """Период из строки. По умолчанию — месяц, за который есть данные."""
    if not spec or spec == "auto":
        return latest_data_month(session, user_id)
    today = date.today()
    if spec == "today":
        return day(today)
    if spec == "week":
        return week(today)
    if spec == "month":
        return month(today.year, today.month)
    if spec == "year":
        return year(today.year)
    try:
        return parse_period(spec, today=today)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


# --------------------------------------------------------------------------
# Mini App
# --------------------------------------------------------------------------


@me.get("/summary", response_model=s.SummaryOut, summary="Всё для главного экрана")
def summary(
    period: str | None = Query(default=None, description="auto|today|week|month|year|2026-09"),
    with_insights: bool = Query(default=True),
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> s.SummaryOut:
    window = _resolve_period(session, user.id, period)
    report = report_for(session, user.id, window)

    items = insights_mod.collect(session, user.id, report) if with_insights else []
    questions = open_questions(session, user.id, limit=10)
    weeks = [
        s.WeekSliceOut(
            label=chunk.label,
            start=chunk.start,
            amount=s.MoneyOut.of(reports_mod.spend_total(session, user.id, chunk)),
        )
        for chunk in split_into_weeks(window)
    ]
    pct = None
    if report.prev_spend_total:
        pct = round(report.spend_delta / report.prev_spend_total * 100, 1)

    return s.SummaryOut(
        period=s.PeriodOut(
            label=window.label, start=window.start, end=window.end, days=window.days
        ),
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
        questions=[s.QuestionOut.of(item) for item in questions],
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


@me.get("/questions", response_model=list[s.QuestionOut], summary="Очередь спорных операций")
def questions(
    limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> list[s.QuestionOut]:
    return [s.QuestionOut.of(item) for item in open_questions(session, user.id, limit=limit)]


@me.post("/questions/{review_id}/answer", summary="Ответить одним тапом")
def answer(
    review_id: int,
    payload: s.AnswerIn,
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> dict:
    item = answer_question(session, user, review_id, payload.code)
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
    period: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    only_spending: bool = Query(default=False),
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
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
    return [s.OperationOut.of(op) for op in session.scalars(query).all()]


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
        session, user, payload.text, when=payload.occurred_at
    )
    if row is None:
        return s.IngestResultOut(ok=False, error=outcome.error)
    return _ingest_out(outcome)


@me.post(
    "/statements",
    response_model=s.IngestResultOut,
    summary="Загрузить свою выписку из Mini App",
)
async def upload_statement(
    file: UploadFile = File(..., description="CSV-выписка банка"),
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> s.IngestResultOut:
    """То же, что прислать файл боту, но не выходя из приложения.

    Без этого Mini App умел бы только показывать, а класть данные пришлось бы
    возвращаться в чат — лишний шаг там, где мы обещаем минимум действий.
    """
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "файл больше 12 МБ")
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "пустой файл")
    outcome = ingest_statement(session, user, data, file.filename or "statement.csv")
    return _ingest_out(outcome)


@me.post(
    "/images",
    response_model=s.IngestResultOut,
    summary="Загрузить скриншот банка или фото чека из Mini App",
)
async def upload_images(
    files: list[UploadFile] = File(..., description="Скриншоты или фотографии чеков"),
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> s.IngestResultOut:
    blobs: list[bytes] = []
    for item in files[:10]:
        data = await item.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"{item.filename} больше 12 МБ"
            )
        if data:
            blobs.append(data)
    if not blobs:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "пустая загрузка")

    outcome = ingest_screenshot(session, user, blobs)
    result = _ingest_out(outcome)
    result.source = {
        "receipt": "Чек",
        "statement": "Скриншот",
    }.get(outcome.image_kind, result.source or "Картинка")
    return result


@me.get("/checkup", summary="Сходятся ли цифры")
def checkup(
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> dict:
    window = latest_data_month(session, user.id)
    total = reports_mod.spend_total(session, user.id, window)
    by_weeks = sum(
        reports_mod.spend_total(session, user.id, chunk) for chunk in split_into_weeks(window)
    )
    result = run_all(session, user.id)
    return {
        "ok": result.ok and by_weeks == total,
        "weeks_sum_matches_month": by_weeks == total,
        "month_total_kopecks": total,
        "weeks_total_kopecks": by_weeks,
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

    user = _target_user(session, tg_id, display_name)
    outcome = ingest_screenshot(session, user, blobs)
    log.info("заливка скриншотов для %s: %s", tg_id, outcome.error or "ок")
    return _ingest_out(outcome)


@ingest.post(
    "/statement",
    response_model=s.IngestResultOut,
    summary="Залить CSV-выписку за пользователя",
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
    outcome = ingest_statement(session, user, data, file.filename or "statement.csv")
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
        session, user, text, source=OperationSource.MANUAL, when=occurred_at
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
