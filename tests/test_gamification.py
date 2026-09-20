"""Геймификация: опыт, достижения, партнёрские задания и карта месяца.

Проверяется одно свойство, из которого следуют все остальные: ни одно
игровое число нельзя получить, не сделав то, за что оно даётся. Опыт
считается из таблицы действий, достижение — из запроса к данным, прогресс
партнёрского задания — из настоящих проводок по этому продавцу, а суммы
станций карты обязаны совпасть с недельными срезами отчёта.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import select

from app.analytics import reports
from app.analytics.periods import month, split_into_weeks
from app.gamification import (
    PARTNER_QUESTS,
    XP_PER_ACTIVE_DAY,
    XP_PER_BEST_STREAK_DAY,
    XP_PER_IMPORT,
    XP_PER_MANUAL,
    XP_PER_TAP,
    Facts,
    PartnerQuest,
    achievements,
    collect_facts,
    game_state,
    level_for,
    level_of,
    quest_deadline,
    quest_progress,
    quest_window,
    total_xp,
    xp_for_level,
)
from app.models import Counterparty, Operation, Posting, User
from app.service import add_quick_expense, record_action
from tests.conftest import seed_user

# Момент, от которого считается карта: середина сентября демонстрационного
# набора. Фиксируем его явно — иначе тест начал бы зависеть от календаря
# машины, на которой запущен.
NOW = datetime(2026, 9, 20, 12, 0)
SEPTEMBER = month(2026, 9)

GAME_TG_ID = 555_001
FRESH_TG_ID = 555_002
SPEND_TG_ID = 555_003
API_TG_ID = 555_004


@pytest.fixture(scope="module", autouse=True)
def _schema(_isolated_db) -> None:
    """Схема на месте, даже когда этот файл гоняют отдельно от остальных."""
    from app.db import init_db

    init_db()


@pytest.fixture(scope="module")
def game_user(_schema) -> int:
    """Свой пользователь с демонстрационным набором: тесты его не меняют."""
    return seed_user(GAME_TG_ID)


def facts_of(session, user_id: int, *, period=SEPTEMBER, now: datetime = NOW) -> Facts:
    user = session.get(User, user_id)
    return collect_facts(session, user, period, now=now)


# --------------------------------------------------------------------------
# Опыт и уровни
# --------------------------------------------------------------------------


def test_лестница_уровней_растёт_и_обратима():
    """Порог уровня и уровень по опыту — взаимно обратные функции."""
    assert xp_for_level(0) == 0
    assert [xp_for_level(n) for n in range(1, 6)] == [100, 300, 600, 1000, 1500]
    for number in range(0, 12):
        floor = xp_for_level(number)
        assert level_for(floor) == number, "ровно на пороге уровень уже получен"
        assert level_for(floor + 1) == number
        if number:
            assert level_for(floor - 1) == number - 1, "рубль до порога — ещё прошлый уровень"


def test_на_пустой_базе_опыта_нет_ни_капли(session):
    """Новый человек не получает ни опыта, ни достижений авансом."""
    from app.service import get_or_create_user

    user = get_or_create_user(session, FRESH_TG_ID, "Новичок")
    session.flush()
    facts = collect_facts(session, user, SEPTEMBER, now=NOW)

    assert total_xp(facts) == 0
    assert not any(a.earned for a in achievements(facts))
    assert level_of(0).number == 0


def test_опыт_растёт_от_действий_и_ровно_на_их_вес(session):
    """Каждое записанное действие добавляет свой вес, и ничего сверх него."""
    from app.service import get_or_create_user

    user = get_or_create_user(session, FRESH_TG_ID + 10, "Счётчик")
    session.flush()

    before = total_xp(collect_facts(session, user, SEPTEMBER, now=NOW))
    assert before == 0

    # Первый тап: сам тап, первый активный день и первый день серии.
    record_action(session, user, taps=1)
    session.flush()
    after_tap = total_xp(collect_facts(session, user, SEPTEMBER, now=NOW))
    assert after_tap == XP_PER_TAP + XP_PER_ACTIVE_DAY + XP_PER_BEST_STREAK_DAY

    # Загрузка и ручной ввод в тот же день: новый день и серия не начисляются.
    record_action(session, user, imports=1, manual=1)
    session.flush()
    after_all = total_xp(collect_facts(session, user, SEPTEMBER, now=NOW))
    assert after_all == after_tap + XP_PER_IMPORT + XP_PER_MANUAL


def test_уровень_и_полоска_считаются_от_опыта():
    """Полоска — доля пути между порогами, а не процент от абсолютного числа."""
    level = level_of(1250)
    assert level.number == 4
    assert level.xp_total_next == 1500
    assert level.progress == pytest.approx((1250 - 1000) / (1500 - 1000))

    assert level_of(0).progress == 0.0
    assert level_of(1500).progress == 0.0, "новый уровень начинается с пустой полоски"


# --------------------------------------------------------------------------
# Достижения
# --------------------------------------------------------------------------


def by_code(items) -> dict:
    return {a.code: a for a in items}


def test_достижение_выдаётся_только_при_выполнении_условия():
    """Три банка — это три банка. На двух достижения нет, но виден прогресс."""
    two = by_code(achievements(Facts(period=SEPTEMBER, now=NOW, banks_with_postings=2)))
    three = by_code(achievements(Facts(period=SEPTEMBER, now=NOW, banks_with_postings=3)))

    assert two["three_banks"].earned is False
    assert two["three_banks"].xp == 0
    assert two["three_banks"].progress == pytest.approx(2 / 3)
    assert two["three_banks"].hint, "невыданное достижение обязано объяснить, чего не хватает"

    assert three["three_banks"].earned is True
    assert three["three_banks"].xp == 80
    assert three["three_banks"].progress == 1.0
    assert three["three_banks"].hint is None


def test_ступень_даёт_опыт_только_своей_ступени():
    """Рекорд в 18 дней — это вторая ступень, а не сумма первой и второй."""
    six = by_code(achievements(Facts(period=SEPTEMBER, now=NOW, best_streak=6)))["streak"]
    eighteen = by_code(achievements(Facts(period=SEPTEMBER, now=NOW, best_streak=18)))["streak"]
    forever = by_code(achievements(Facts(period=SEPTEMBER, now=NOW, best_streak=40)))["streak"]

    assert six.earned is False and six.xp == 0
    assert eighteen.earned is True and eighteen.xp == 120
    assert "30" in eighteen.detail, "следующая ступень должна быть названа"
    assert forever.xp == 250
    assert "ступень" not in forever.detail, "выше верхней ступени звать некуда"


def test_экономный_месяц_не_выдаётся_авансом():
    """Третьего числа траты заведомо меньше прошлого месяца — это не заслуга."""
    running = Facts(
        period=SEPTEMBER, now=NOW, spend_current=100, spend_previous=10_000,
        period_closed=False,
    )
    closed = Facts(
        period=SEPTEMBER, now=NOW, spend_current=9_000, spend_previous=10_000,
        period_closed=True,
    )
    overspent = Facts(
        period=SEPTEMBER, now=NOW, spend_current=11_000, spend_previous=10_000,
        period_closed=True,
    )

    assert by_code(achievements(running))["thrifty_month"].earned is False
    assert by_code(achievements(closed))["thrifty_month"].earned is True
    assert by_code(achievements(overspent))["thrifty_month"].earned is False


def test_чистая_доска_не_выдаётся_за_пустую_базу():
    """Ноль вопросов бывает и оттого, что данных нет вовсе."""
    empty = Facts(period=SEPTEMBER, now=NOW, open_questions=0, raw_rows=0)
    clean = Facts(period=SEPTEMBER, now=NOW, open_questions=0, raw_rows=104)
    pending = Facts(period=SEPTEMBER, now=NOW, open_questions=3, raw_rows=104)

    assert by_code(achievements(empty))["clean_board"].earned is False
    assert by_code(achievements(clean))["clean_board"].earned is True
    assert by_code(achievements(pending))["clean_board"].earned is False
    assert "3" in by_code(achievements(pending))["clean_board"].hint


def test_осознанность_пустой_книги_не_считается_достижением():
    """Без трат «сто процентов ясности» — арифметика, а не результат человека."""
    empty = Facts(period=SEPTEMBER, now=NOW, awareness_percent=100, spend_current=0)
    real = Facts(period=SEPTEMBER, now=NOW, awareness_percent=99, spend_current=12_561_820)

    assert by_code(achievements(empty))["awareness"].earned is False
    assert by_code(achievements(real))["awareness"].earned is True


def test_дата_получения_не_выдумывается():
    """Там, где даты в данных нет, поле пустое, а не «сегодня»."""
    facts = Facts(period=SEPTEMBER, now=NOW, banks_with_postings=3, months_covered=3)
    items = by_code(achievements(facts))
    assert items["three_banks"].earned is True
    assert items["three_banks"].earned_at is None
    assert items["three_months"].earned_at is None


def test_на_демонстрационном_наборе_закрыто_не_всё(session, game_user):
    """Две незакрытые ачивки из десяти — это проверяемое состояние, а не витрина."""
    items = by_code(achievements(facts_of(session, game_user)))
    assert len(items) == 10

    # «Чистая доска» ведёт ровно в основной цикл продукта: ответить на вопросы.
    assert items["clean_board"].earned is False
    # «Экономный месяц» нельзя выдать, пока месяц не закончился.
    assert items["thrifty_month"].earned is False

    assert items["first_import"].earned is True
    assert items["first_import"].earned_at is not None, "дата первой загрузки в базе есть"
    assert items["three_banks"].earned is True
    assert items["three_months"].earned is True
    assert items["refund_found"].earned is True


# --------------------------------------------------------------------------
# Партнёрские задания
# --------------------------------------------------------------------------


def test_партнёрское_задание_без_условия_не_заводится():
    """Задание, совпадающее с любой тратой, выполнялось бы само собой."""
    with pytest.raises(ValueError):
        PartnerQuest(
            code="bad", sponsor="Кто-то", title="?", goal="?", reward="?",
            xp=10, ends_day=30, min_amount=100,
        )
    with pytest.raises(ValueError):
        PartnerQuest(
            code="bad", sponsor="Кто-то", title="?", goal="?", reward="?",
            xp=10, ends_day=30, match_key="surf coffee",
        )


def test_прогресс_партнёрского_задания_считается_из_настоящих_трат(session, game_user):
    """Сумма задания обязана совпасть с проводками по этому продавцу.

    Считаем то же самое вторым способом — перебором проводок в питоне, а не
    агрегатом в SQL: если прогресс когда-нибудь начнут «дорисовывать», эти
    два числа разойдутся.
    """
    offer = next(q for q in PARTNER_QUESTS if q.code == "surf_coffee_2000")
    window = quest_window(SEPTEMBER, quest_deadline(SEPTEMBER, offer.ends_day))
    amount, count = quest_progress(session, game_user, offer, window)

    rows = session.execute(
        select(Posting, Counterparty.norm_key)
        .join(Operation, Operation.id == Posting.operation_id)
        .outerjoin(Counterparty, Counterparty.id == Operation.counterparty_id)
        .where(
            Posting.user_id == game_user,
            Posting.account_kind == "expense",
            Posting.at >= window.start,
            Posting.at < window.end,
        )
    ).all()
    expected = [p for p, key in rows if key == offer.match_key]

    assert amount == sum(p.amount for p in expected)
    assert count == len({p.operation_id for p in expected})
    assert amount > 0, "в демонстрационном наборе покупки у этого продавца есть"


def test_задание_без_трат_показывает_ноль_а_не_придуманный_прогресс(session, game_user):
    """Партнёра, у которого человек ничего не купил, нельзя показать начатым."""
    state = game_state(session, session.get(User, game_user), SEPTEMBER, now=NOW)
    dodo = next(q for q in state.quests if q.code == "dodo_700")

    assert dodo.kind == "sponsor"
    assert dodo.progress == 0.0
    assert dodo.done is False
    assert dodo.sponsor and dodo.reward, "у партнёрского задания есть спонсор и награда"


def test_настоящая_покупка_двигает_прогресс_задания(_isolated_db):
    """Трата у партнёра приезжает из данных — и прогресс меняется сама.

    Никакого «засчитать выполнение» в коде нет: задание закрывается тем же
    путём, каким закрылось бы в жизни, — покупкой, попавшей в книгу.
    """
    from app.db import session_scope

    user_id = seed_user(SPEND_TG_ID)
    offer = next(q for q in PARTNER_QUESTS if q.code == "surf_coffee_2000")
    window = quest_window(SEPTEMBER, quest_deadline(SEPTEMBER, offer.ends_day))

    with session_scope() as session:
        before, _ = quest_progress(session, user_id, offer, window)
        user = session.get(User, user_id)
        row, outcome = add_quick_expense(
            session, user, "surf coffee 500", when=datetime(2026, 9, 10, 9, 30)
        )
        assert row is not None and outcome.ok, outcome.error

    with session_scope() as session:
        after, _ = quest_progress(session, user_id, offer, window)
        assert after == before + 50_000

        quest = next(
            q
            for q in game_state(
                session, session.get(User, user_id), SEPTEMBER, now=NOW
            ).quests
            if q.code == offer.code
        )
        assert quest.progress > 0
        assert quest.progress == pytest.approx(min(1.0, after / offer.min_amount))


def test_срок_задания_обрезается_границами_периода():
    """Предложение не может действовать дольше месяца, который мы показываем."""
    september = month(2026, 9)
    assert quest_deadline(september, 31) == date(2026, 9, 30)
    assert quest_deadline(september, 12) == date(2026, 9, 12)

    window = quest_window(september, date(2026, 9, 12))
    assert window.start == september.start
    assert window.end == datetime(2026, 9, 13), "дедлайн входит в окно целиком"


# --------------------------------------------------------------------------
# Карта месяца
# --------------------------------------------------------------------------


def test_карта_месяца_сходится_с_недельными_срезами(session, game_user):
    """Станции-недели — это те же полуинтервалы, что и в отчёте.

    Тот же инвариант, что у всего проекта: сумма недель равна месяцу. Карта
    не может показывать одно, а вкладка отчётов другое.
    """
    state = game_state(session, session.get(User, game_user), SEPTEMBER, now=NOW)
    stations = [st for st in state.map if st.kind == "week"]
    chunks = split_into_weeks(SEPTEMBER)

    assert len(stations) == len(chunks)
    for station, chunk in zip(stations, chunks, strict=True):
        assert station.detail == chunk.label
        assert station.amount == reports.spend_total(session, game_user, chunk)

    assert sum(st.amount for st in stations) == reports.spend_total(
        session, game_user, SEPTEMBER
    )


def test_грош_стоит_ровно_на_одной_неделе(session, game_user):
    """Текущая станция одна — та, в которую попадает сегодняшний день."""
    state = game_state(session, session.get(User, game_user), SEPTEMBER, now=NOW)
    weeks = [st for st in state.map if st.kind == "week"]
    current = [st for st in weeks if st.state == "current"]

    assert len(current) == 1
    index = weeks.index(current[0])
    assert split_into_weeks(SEPTEMBER)[index].contains(NOW)
    assert all(st.state == "done" for st in weeks[:index])
    assert all(st.state == "locked" for st in weeks[index + 1 :])


def test_закрытый_месяц_пройден_целиком(session, game_user):
    """В прошлом месяце текущей недели нет, а финиш показывает итог, не прогноз."""
    state = game_state(session, session.get(User, game_user), month(2026, 7), now=NOW)
    weeks = [st for st in state.map if st.kind == "week"]
    finish = state.map[-1]

    assert weeks and all(st.state == "done" for st in weeks)
    assert finish.kind == "finish"
    assert finish.state == "done"
    assert finish.amount == reports.spend_total(session, game_user, month(2026, 7))


def test_спорные_операции_стоят_станциями_и_ведут_к_вопросу(session, game_user):
    """Станция вопроса открывает ровно тот вопрос, который на ней написан."""
    from app.service import open_questions

    state = game_state(session, session.get(User, game_user), SEPTEMBER, now=NOW)
    stations = [st for st in state.map if st.kind == "question"]
    pending = {item.id: item for item in open_questions(session, game_user, limit=50)}

    assert stations, "в демонстрационном наборе открытые вопросы есть"
    for station in stations:
        assert station.action == {"type": "question", "id": int(station.code[1:])}
        assert station.action["id"] in pending
        assert station.detail == pending[station.action["id"]].question
        assert station.state == "current", "открытый вопрос закрывается прямо сейчас"


def test_карта_состоит_только_из_описанных_видов_станций(session, game_user):
    """Контракт перечисляет пять видов и три состояния — других быть не должно."""
    state = game_state(session, session.get(User, game_user), SEPTEMBER, now=NOW)

    assert {st.kind for st in state.map} <= {"week", "question", "chest", "quest", "finish"}
    assert {st.state for st in state.map} <= {"done", "current", "locked"}
    assert [st.code for st in state.map] == list(dict.fromkeys(st.code for st in state.map))
    assert state.map[-1].kind == "finish"

    chests = [st for st in state.map if st.kind == "chest"]
    earned = {a.title for a in state.achievements if a.earned}
    for chest in chests:
        assert (chest.state == "done") == (chest.title in earned)


def test_сундуки_позади_собраны_а_впереди_ещё_нет(session, game_user):
    """Тропа рассказывает историю: слева сделанное, справа то, до чего идти."""
    state = game_state(session, session.get(User, game_user), SEPTEMBER, now=NOW)
    position = next(
        i
        for i, st in enumerate(state.map)
        if st.kind == "week" and st.state == "current"
    )
    behind = [st for i, st in enumerate(state.map) if st.kind == "chest" and i < position]
    ahead = [st for i, st in enumerate(state.map) if st.kind == "chest" and i > position]

    assert behind and ahead, "на тропе должны быть и собранные сундуки, и цели впереди"
    assert all(st.state == "done" for st in behind)
    assert all(st.state == "locked" for st in ahead)


# --------------------------------------------------------------------------
# Состояние целиком
# --------------------------------------------------------------------------


def test_состояние_идемпотентно(session, game_user):
    """Два запроса подряд дают одно и то же: опыт нигде не накапливается."""
    user = session.get(User, game_user)
    first = game_state(session, user, SEPTEMBER, now=NOW)
    second = game_state(session, user, SEPTEMBER, now=NOW)

    assert first.level.xp == second.level.xp
    assert [q.progress for q in first.quests] == [q.progress for q in second.quests]
    assert [st.state for st in first.map] == [st.state for st in second.map]


def test_опыт_складывается_из_действий_серии_и_достижений(session, game_user):
    """Сумма разложена по слагаемым — на защите её можно проверить руками."""
    facts = facts_of(session, game_user)
    items = achievements(facts)

    base = (
        XP_PER_TAP * facts.taps
        + XP_PER_MANUAL * facts.manual_entries
        + XP_PER_IMPORT * facts.imports
        + XP_PER_ACTIVE_DAY * facts.active_days
    )
    assert total_xp(facts, items) == (
        base + XP_PER_BEST_STREAK_DAY * facts.best_streak
        + sum(a.xp for a in items if a.earned)
    )


def test_задания_делятся_на_наши_и_партнёрские(session, game_user):
    """У партнёрского есть спонсор и награда, у нашего их нет."""
    state = game_state(session, session.get(User, game_user), SEPTEMBER, now=NOW)

    assert {q.kind for q in state.quests} == {"system", "sponsor"}
    for quest in state.quests:
        assert 0.0 <= quest.progress <= 1.0
        assert quest.done == (quest.progress >= 1.0)
        assert quest.deadline is not None
        assert quest.deadline < SEPTEMBER.end.date()
        if quest.kind == "sponsor":
            assert quest.sponsor and quest.reward
        else:
            assert quest.sponsor is None and quest.reward is None


# --------------------------------------------------------------------------
# Эндпоинт
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client(_schema):
    """Только роутер геймификации: подключение к общему приложению — дело координатора."""
    import os

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tests.test_api import BOT_TOKEN

    seed_user(API_TG_ID)
    was = os.environ.get("BOT_TOKEN")
    os.environ["BOT_TOKEN"] = BOT_TOKEN

    from app.config import get_settings

    get_settings.cache_clear()
    from app.api.game import game

    app = FastAPI()
    app.include_router(game)
    with TestClient(app) as test_client:
        yield test_client

    if was is None:
        os.environ.pop("BOT_TOKEN", None)
    else:
        os.environ["BOT_TOKEN"] = was
    get_settings.cache_clear()


def auth(tg_id: int = API_TG_ID) -> dict[str, str]:
    from tests.test_api import make_init_data

    return {"X-Telegram-Init-Data": make_init_data(tg_id)}


def test_без_подписи_игровое_состояние_не_отдаётся(client):
    assert client.get("/api/v1/me/game").status_code == 401


def test_эндпоинт_отдаёт_контракт_целиком(client):
    """Форма ответа согласована с фронтом и проверяется по полям, а не на глаз."""
    data = client.get("/api/v1/me/game?period=2026-09", headers=auth()).json()

    assert set(data) == {"level", "quests", "achievements", "map"}

    level = data["level"]
    assert set(level) == {"number", "title", "xp", "xp_total_next", "progress"}
    assert level["xp"] >= 0 and level["xp_total_next"] > level["xp"]
    assert 0.0 <= level["progress"] <= 1.0

    for quest in data["quests"]:
        assert set(quest) == {
            "code", "kind", "title", "detail", "progress",
            "xp", "done", "sponsor", "reward", "deadline",
        }
        assert quest["kind"] in {"system", "sponsor"}

    for item in data["achievements"]:
        assert set(item) == {
            "code", "title", "detail", "earned", "earned_at", "progress", "hint"
        }
        assert item["earned"] is not (item["hint"] is not None)
        if item["earned"]:
            assert item["progress"] == 1.0

    for station in data["map"]:
        assert set(station) == {
            "code", "kind", "state", "title", "detail", "amount", "action"
        }
        assert station["kind"] in {"week", "question", "chest", "quest", "finish"}
        assert station["state"] in {"done", "current", "locked"}
        if station["amount"] is not None:
            assert set(station["amount"]) == {"kopecks", "text"}
            assert station["amount"]["text"].endswith("₽")


def test_чужое_игровое_состояние_не_видно(client):
    """Другой telegram id — чистый лист, а не чужой прогресс."""
    data = client.get("/api/v1/me/game?period=2026-09", headers=auth(919_191)).json()

    assert data["level"]["xp"] == 0
    assert data["level"]["number"] == 0
    assert not any(a["earned"] for a in data["achievements"])
    assert all(q["progress"] == 0.0 for q in data["quests"] if q["kind"] == "sponsor")


def test_суммы_станций_совпадают_со_сводкой(client):
    """Карта месяца и отчёт считают по одним и тем же проводкам."""
    game_data = client.get("/api/v1/me/game?period=2026-09", headers=auth()).json()
    weeks = [st for st in game_data["map"] if st["kind"] == "week"]

    from app.analytics.periods import month as month_of
    from app.analytics.reports import spend_total
    from app.db import session_scope
    from app.models import User as UserModel

    with session_scope() as session:
        user = session.scalar(select(UserModel).where(UserModel.tg_id == API_TG_ID))
        total = spend_total(session, user.id, month_of(2026, 9))

    assert sum(st["amount"]["kopecks"] for st in weeks) == total
