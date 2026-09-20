"""Разрезы за период — всё, что нужно вкладке «Отчёты», одним запросом.

Отдельный эндпоинт, а не раздувание `/summary`: главный экран открывается
при каждом запуске, а разрезы нужны, только когда человек открыл отчёты.

Все числа считаются теми же функциями, что и итог на главной, и все — по
проводкам. Поэтому топ продавцов, дни недели и средние чеки складываются
ровно в ту сумму, которая написана на главном экране, а не «примерно
совпадают»: это одно множество проводок, разрезанное по-разному.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.analytics import reports as reports_mod
from app.analytics.periods import (
    GRANULARITY_TITLE,
    Period,
    day,
    month,
    parse_period,
    week,
    year,
)
from app.api import schemas as s
from app.api.auth import current_user, db_session
from app.models import User
from app.service import latest_data_month

analytics = APIRouter(tags=["Аналитика"])

PERIOD_HELP = "auto|today|week|month|year|2026|2026-09|2026-09-15|2026-W38"


def resolve_period(session: Session, user_id: int, spec: str | None) -> Period:
    """Период из строки запроса. По умолчанию — месяц, за который есть данные.

    Один разбор на все эндпоинты: если бы /summary и /analytics понимали
    `period` по-разному, вкладка «Отчёты» показывала бы другой месяц, чем
    главный экран, и обе цифры были бы правдой по отдельности.
    """
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


@analytics.get("/analytics", response_model=s.AnalyticsOut, summary="Разрезы за период")
def period_analytics(
    period: str | None = Query(default=None, description=PERIOD_HELP),
    top: int = Query(default=10, ge=1, le=50, description="Длина списков «топ»"),
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> s.AnalyticsOut:
    window = resolve_period(session, user.id, period)
    previous = window.shifted_back()

    granularity, parts = reports_mod.slice_breakdown(session, user.id, window)
    spend = reports_mod.spend_total(session, user.id, window)
    prev_spend = reports_mod.spend_total(session, user.id, previous)
    delta = spend - prev_spend

    merchants = reports_mod.spend_by_merchant(session, user.id, window)
    weekdays = reports_mod.spend_by_weekday(session, user.id, window)
    peak = reports_mod.peak_weekday(weekdays)
    checks = reports_mod.average_check_by_category(session, user.id, window)
    subscriptions = reports_mod.subscription_lines(session, user.id, window)
    monthly, yearly = reports_mod.subscriptions_cost(subscriptions)
    changes = reports_mod.category_changes(session, user.id, window, previous=previous)
    additivity = reports_mod.additivity_checks(session, user.id, window)

    # Категории берём из того же сравнения, что и «выросло/упало»: иначе
    # пришлось бы второй раз считать предыдущий период ради тех же чисел.
    categories = sorted(
        (line for line in changes if line.amount),
        key=lambda line: line.amount,
        reverse=True,
    )

    return s.AnalyticsOut(
        period=s.period_out(window),
        prev_period=s.period_out(previous),
        granularity=granularity,
        granularity_title=GRANULARITY_TITLE[granularity],
        slices=[s.slice_out(part, amount) for part, amount in parts],
        spend=s.MoneyOut.of(spend),
        prev_spend=s.MoneyOut.of(prev_spend),
        spend_delta=s.MoneyOut.of(delta, sign=True),
        spend_delta_pct=(round(delta / prev_spend * 100, 1) if prev_spend else None),
        avg_per_day=s.MoneyOut.of(spend // max(1, window.days)),
        categories=[
            s.CategoryOut(
                code=line.code,
                title=line.title,
                emoji=line.emoji,
                essential=line.essential,
                amount=s.MoneyOut.of(line.amount),
                share=round(line.amount / spend, 4) if spend else 0.0,
                prev_amount=s.MoneyOut.of(line.prev_amount),
                delta=s.MoneyOut.of(line.delta, sign=True),
                delta_pct=(round(line.delta_pct, 1) if line.delta_pct is not None else None),
            )
            for line in categories
        ],
        top_merchants=[s.merchant_out(line) for line in merchants[:top]],
        merchants_total=len(merchants),
        weekdays=[s.weekday_out(line) for line in weekdays],
        peak_weekday=(s.weekday_out(peak) if peak else None),
        average_checks=[s.average_check_out(line) for line in checks],
        subscriptions=[s.subscription_out(line) for line in subscriptions],
        subscriptions_monthly=s.MoneyOut.of(monthly),
        subscriptions_yearly=s.MoneyOut.of(yearly),
        grew=[
            s.category_change_out(line)
            for line in changes
            if line.direction in {"up", "new"}
        ][:top],
        fell=[
            s.category_change_out(line)
            for line in changes
            if line.direction in {"down", "gone"}
        ][:top],
        additivity=[s.additivity_out(check) for check in additivity],
        additivity_ok=all(check.ok for check in additivity),
    )
