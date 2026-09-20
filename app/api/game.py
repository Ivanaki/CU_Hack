"""Геймификация для Mini App: уровень, задания, достижения и карта месяца.

Отдельный модуль по образцу app/api/quick.py: свой роутер, свои формы
ответов рядом в файле, расчёт — в app/gamification.py. В routes.py это не
поместилось бы без превращения сводки в свалку: у экрана карты своя
модель данных и свой период.

Аутентификация та же, что у остальных /me/*: подпись Telegram через
Depends(current_user). Служебный X-API-Key сюда не подходит принципиально —
по нему можно читать и писать данные любого пользователя, и в браузер он
попасть не должен.

Подключение — одна строка в app/api/app.py, рядом с остальными роутерами и
обязательно ДО `app.mount("/", StaticFiles...)`: статика примонтирована на
корень и перехватывает всё, что добавлено после неё (маршрут ответит
«405 Method Not Allowed», и это не сразу очевидно).

    from app.api.game import game    # к остальным импортам
    app.include_router(game)         # рядом с include_router(uploads)
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import gamification as game_mod
from app.analytics.periods import Period, day, month, parse_period, week, year
from app.api import schemas as s
from app.api.auth import current_user, db_session
from app.models import User
from app.service import latest_data_month

game = APIRouter(prefix="/api/v1/me", tags=["Геймификация"])


# --------------------------------------------------------------------------
# Формы ответа
# --------------------------------------------------------------------------


class LevelOut(BaseModel):
    """Уровень и полоска до следующего. `progress` — доля, 0..1."""

    number: int
    title: str
    xp: int
    xp_total_next: int
    progress: float = Field(ge=0.0, le=1.0)


class QuestOut(BaseModel):
    """Задание. kind=sponsor — партнёрское, и тогда заполнены sponsor и reward."""

    code: str
    kind: str
    title: str
    detail: str
    progress: float = Field(ge=0.0, le=1.0)
    xp: int
    done: bool
    sponsor: str | None = None
    reward: str | None = None
    deadline: date | None = None


class AchievementOut(BaseModel):
    """Достижение. hint заполнен, только пока оно не получено."""

    code: str
    title: str
    detail: str
    earned: bool
    earned_at: date | None = None
    progress: float = Field(ge=0.0, le=1.0)
    hint: str | None = None


class StationOut(BaseModel):
    """Станция на карте месяца.

    action — куда ведёт тап: null, {"type":"question","id":12} или
    {"type":"tab","tab":"reports"}.
    """

    code: str
    kind: str
    state: str
    title: str
    detail: str
    amount: s.MoneyOut | None = None
    action: dict | None = None


class GameStateOut(BaseModel):
    """Всё, что нужно экрану карты месяца, одним запросом."""

    level: LevelOut
    quests: list[QuestOut]
    achievements: list[AchievementOut]
    map: list[StationOut]


# --------------------------------------------------------------------------
# Период
# --------------------------------------------------------------------------


def _resolve_period(session: Session, user_id: int, spec: str | None) -> Period:
    """Период из строки. По умолчанию — месяц, за который вообще есть данные.

    Повторяет поведение /me/summary намеренно: карта месяца и сводка обязаны
    открываться на одном и том же месяце, иначе сумма станций не сойдётся с
    суммой на главном экране.
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


# --------------------------------------------------------------------------
# Эндпоинт
# --------------------------------------------------------------------------


@game.get("/game", response_model=GameStateOut, summary="Уровень, задания, ачивки и карта")
def state(
    period: str | None = Query(default=None, description="auto|today|week|month|year|2026-09"),
    user: User = Depends(current_user),
    session: Session = Depends(db_session),
) -> GameStateOut:
    """Игровое состояние целиком.

    Ни одно число здесь не хранится: опыт, достижения и прогресс заданий
    выводятся из фактических данных при каждом запросе. Поэтому ответ
    идемпотентен, а показать прогресс, которого не было, физически нечем.
    """
    window = _resolve_period(session, user.id, period)
    snapshot = game_mod.game_state(session, user, window)

    return GameStateOut(
        level=LevelOut(
            number=snapshot.level.number,
            title=snapshot.level.title,
            xp=snapshot.level.xp,
            xp_total_next=snapshot.level.xp_total_next,
            progress=round(snapshot.level.progress, 4),
        ),
        quests=[
            QuestOut(
                code=q.code,
                kind=q.kind,
                title=q.title,
                detail=q.detail,
                progress=round(q.progress, 4),
                xp=q.xp,
                done=q.done,
                sponsor=q.sponsor,
                reward=q.reward,
                deadline=q.deadline,
            )
            for q in snapshot.quests
        ],
        achievements=[
            AchievementOut(
                code=a.code,
                title=a.title,
                detail=a.detail,
                earned=a.earned,
                earned_at=a.earned_at,
                progress=round(a.progress, 4),
                hint=a.hint,
            )
            for a in snapshot.achievements
        ],
        map=[
            StationOut(
                code=station.code,
                kind=station.kind,
                state=station.state,
                title=station.title,
                detail=station.detail,
                amount=(
                    s.MoneyOut.of(station.amount) if station.amount is not None else None
                ),
                action=station.action,
            )
            for station in snapshot.map
        ],
    )
