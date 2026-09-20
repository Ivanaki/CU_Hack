"""Виджет Shortcuts на рабочем столе iPhone: один тап — одно действие.

Отдельный модуль, а не ещё пара ручек в routes.py, по трём причинам.

Аутентификация третья по счёту. Mini App приходит с подписью Telegram,
служебная заливка — с ключом от нашего же сервиса. Быстрая команда живёт
на телефоне владельца: подписать initData ей нечем, поэтому она носит тот
же служебный ключ. Но после тапа команда открывает веб-страницу с
карточкой, а отдавать служебный ключ в браузер нельзя — по нему пишут
данные любому пользователю. Поэтому ответ несёт короткоживущий токен:
подписан тем же ключом, привязан к одному tg_id, живёт полчаса.

Формы ответов тоже свои. Виджету нужен не полный отчёт на сорок полей, а
одна карточка, у которой все строки уже отформатированы: Shortcuts умеет
достать значение из словаря, но не умеет форматировать деньги, и каждая
такая мелочь — лишний шаг в рецепте, который человек собирает руками.

Подключение — одна строка в app/api/app.py, сразу после
`app.include_router(ingest)` и обязательно ДО `app.mount("/", StaticFiles...)`:
статика примонтирована на корень и перехватывает всё, что добавлено после
неё (маршрут ответит «405 Method Not Allowed», и это не сразу очевидно).

    from app.api.quick import quick   # к остальным импортам
    app.include_router(quick)         # рядом с include_router(ingest)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.analytics.periods import Period
from app.api.auth import db_session
from app.config import Settings, get_settings
from app.models import User, fmt_money
from app.service import (
    add_quick_expense,
    answer_question,
    average_actions_per_day,
    count_open_questions,
    get_or_create_user,
    latest_data_month,
    open_questions,
    report_for,
)

log = logging.getLogger(__name__)

quick = APIRouter(prefix="/api/v1/quick", tags=["Виджет iPhone"])

# Токен живёт ровно столько, сколько человек смотрит на карточку после тапа.
# Больше не нужно: следующий тап выдаст новый.
TOKEN_TTL = 30 * 60

# Страница карточки. Отдаётся той же статикой, что и Mini App, поэтому
# отдельного адреса и отдельного веб-сервера не появляется.
CARD_PAGE = "/w.html"

# Что человек говорит, когда ему нечего записать, а посмотреть хочется.
# Без этого списка «ничего» уехало бы в трату с суммой из слова «ничего».
_NOTHING = {
    "",
    "ничего",
    "нет",
    "пусто",
    "просто посмотреть",
    "посмотреть",
    "итоги",
    "покажи",
    "покажи итоги",
    "отмена",
}


# --------------------------------------------------------------------------
# Токен для страницы карточки
# --------------------------------------------------------------------------


def _sign(body: str, secret: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:24]


def make_token(tg_id: int, secret: str, *, ttl: int = TOKEN_TTL) -> str:
    """Подписанный пропуск на чтение своей карточки.

    Без состояния на сервере: перезапуск контейнера посреди демонстрации не
    должен превращать открытую карточку в 401.
    """
    body = f"{tg_id}.{int(time.time()) + ttl}"
    return f"{body}.{_sign(body, secret)}"


def read_token(token: str, secret: str) -> int:
    parts = token.split(".")
    if len(parts) != 3:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "испорченный токен")
    body = f"{parts[0]}.{parts[1]}"
    if not hmac.compare_digest(_sign(body, secret), parts[2]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "подпись токена не сходится")
    if not parts[0].isdigit() or not parts[1].isdigit():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "испорченный токен")
    if int(parts[1]) < time.time():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "токен просрочен, нажмите виджет снова")
    return int(parts[0])


def _secret(settings: Settings) -> str:
    if not settings.ingest_api_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "виджет выключен: не задан INGEST_API_KEY"
        )
    return settings.ingest_api_key


def widget_user(
    tg_id: int | None = Query(default=None, description="Telegram id, если идём по ключу"),
    t: str = Query(default="", description="токен карточки, выданный после тапа"),
    x_api_key: str = Header(default="", alias="X-API-Key"),
    session: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> User:
    """Пользователь виджета: либо служебный ключ + tg_id, либо токен карточки."""
    secret = _secret(settings)
    if t:
        return get_or_create_user(session, read_token(t, secret))
    if not hmac.compare_digest(x_api_key, secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "нужен X-API-Key или токен карточки")
    if tg_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "по ключу нужен tg_id")
    return get_or_create_user(session, tg_id)


# --------------------------------------------------------------------------
# Карточка
# --------------------------------------------------------------------------


def _base_url(request: Request) -> str:
    """Внешний адрес сервиса.

    За cloudflare-туннелем uvicorn видит обычный http, и собранная по
    request.url ссылка на карточку открылась бы по http — Safari покажет
    предупреждение прямо на защите. Заголовки от прокси тут единственный
    источник правды про схему и домен.
    """
    headers = request.headers
    scheme = headers.get("x-forwarded-proto", "").split(",")[0].strip() or request.url.scheme
    host = headers.get("x-forwarded-host", "").split(",")[0].strip() or headers.get("host", "")
    return f"{scheme}://{host}" if host else str(request.base_url).rstrip("/")


def _days_left(window: Period) -> int:
    return max(0, (window.end.date() - date.today()).days)


def build_card(
    session: Session,
    user: User,
    *,
    request: Request,
    settings: Settings,
    headline: str = "",
) -> dict:
    """Всё, что виджет показывает после тапа, одним словарём.

    Строки приходят уже отформатированными: любое форматирование на стороне
    Shortcuts — это лишний шаг в рецепте, который человек собирает пальцем.
    """
    window = latest_data_month(session, user.id)
    report = report_for(session, user.id, window)
    pending = open_questions(session, user.id, limit=1)
    total_questions = count_open_questions(session, user.id)

    question = None
    if pending:
        item = pending[0]
        op = item.operation
        question = {
            "id": item.id,
            "text": item.question,
            "amount": fmt_money(op.amount) if op else "",
            "explanation": (op.explanation if op else ""),
            "options": [
                {"code": o["code"], "label": o["label"]} for o in (item.options or [])
            ],
        }

    forecast = ""
    if report.forecast is not None and report.forecast.is_meaningful:
        forecast = f"к концу месяца ≈ {fmt_money(report.forecast.projected)}"

    spent = fmt_money(report.spend_total)
    awareness = report.awareness.percent
    token = make_token(user.tg_id, _secret(settings))

    # Одна готовая строка для «Показать уведомление»: если карточку не
    # открывать, человек всё равно видит итог тапа.
    notification = " · ".join(
        x
        for x in (
            headline,
            f"{window.label}: {spent}",
            f"осознанность {awareness} %",
            (f"вопрос: {total_questions}" if total_questions else "вопросов нет"),
        )
        if x
    )

    return {
        "ok": True,
        "headline": headline,
        "period": window.label,
        "spent": spent,
        "spent_kopecks": int(report.spend_total),
        "income": fmt_money(report.income_total),
        "awareness": awareness,
        "unclear": fmt_money(report.awareness.unclear_volume),
        "days_left": _days_left(window),
        "forecast": forecast,
        "open_questions": total_questions,
        "question": question,
        "actions_per_day": round(average_actions_per_day(session, user.id), 2),
        "streak_days": user.streak_days,
        "notification": notification,
        "token": token,
        # Итог тапа едет в адресе: страница читает карточку заново и сама по
        # себе уже не знает, что именно человек только что продиктовал.
        "card_url": (
            f"{_base_url(request)}{CARD_PAGE}?t={token}"
            + (f"&h={quote(headline)}" if headline else "")
        ),
    }


# --------------------------------------------------------------------------
# Эндпоинты
# --------------------------------------------------------------------------


class TapIn(BaseModel):
    tg_id: int
    # Продиктованное. Пусто — человек просто открыл виджет посмотреть.
    text: str = Field(default="", max_length=200, examples=["кофе 300"])
    display_name: str = ""


@quick.post("/tap", summary="Тап по виджету: записать наличные и вернуть карточку")
def tap(
    payload: TapIn,
    request: Request,
    session: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
    x_api_key: str = Header(default="", alias="X-API-Key"),
) -> dict:
    """Единственный запрос, который делает быстрая команда.

    Пустой текст — не ошибка: тап без диктовки означает «покажи, что там»,
    и в счётчик действий он не попадает. Метрика «действий в день» считает
    то, чего инструмент требует от человека, а посмотреть на цифры человек
    приходит сам — засчитывать это себе в требуемые действия нечестно.
    """
    if not hmac.compare_digest(x_api_key, _secret(settings)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "неверный X-API-Key")

    user = get_or_create_user(session, payload.tg_id, payload.display_name)
    said = (payload.text or "").strip()
    headline = ""
    error = ""

    if said.lower().strip(" .,!?") not in _NOTHING:
        row, outcome = add_quick_expense(session, user, said)
        if row is None:
            error = outcome.error
            headline = f"Не разобрал: «{said}»"
        else:
            headline = f"Записал: {row.description} — {fmt_money(-row.amount)}"

    card = build_card(session, user, request=request, settings=settings, headline=headline)
    card["said"] = said
    card["error"] = error
    log.info("тап виджета от %s: %s", payload.tg_id, error or headline or "просмотр")
    return card


@quick.get("/card", summary="Карточка виджета")
def card(
    request: Request,
    user: User = Depends(widget_user),
    session: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    return build_card(session, user, request=request, settings=settings)


class QuickAnswerIn(BaseModel):
    review_id: int
    code: str = Field(min_length=1, max_length=64)


@quick.post("/answer", summary="Ответ на спорную операцию прямо с карточки")
def answer(
    payload: QuickAnswerIn,
    request: Request,
    user: User = Depends(widget_user),
    session: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Тот же один тап, что в боте, но не покидая карточки виджета."""
    item = answer_question(session, user, payload.review_id, payload.code)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "вопрос не найден или уже закрыт")
    return build_card(
        session, user, request=request, settings=settings, headline="Ответ принят"
    )
