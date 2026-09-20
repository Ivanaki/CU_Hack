"""Вшивает в Mini App запасной набор данных, посчитанный настоящим движком.

Зачем: страница обязана что-то показывать, когда API недоступен — но
показывать она должна не выдуманные числа, а те же, что выдаёт бэкенд.
Написанные руками заглушки неизбежно разъезжаются: первая же версия
показала «расхождение недель и месяца» на экране, который как раз и
доказывает, что расхождений не бывает.

    python -m scripts.build_miniapp
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.analytics import insights as insights_mod  # noqa: E402
from app.analytics import reports as reports_mod  # noqa: E402
from app.analytics.periods import month, split_into_weeks  # noqa: E402
from app.api import schemas as s  # noqa: E402
from app.api.game import state_out  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.gamification import game_state  # noqa: E402
from app.models import User  # noqa: E402
from app.service import average_actions_per_day, count_open_questions, open_questions  # noqa: E402

TARGET = ROOT / "miniapp" / "dist" / "index.html"
DEMO_PERIOD = month(2026, 9)
MARKER = re.compile(r"const FALLBACK = \{.*?\};\n", re.DOTALL)


def build_payload(session, user: User) -> dict:
    report = reports_mod.build_report(session, user.id, DEMO_PERIOD)
    weeks = [
        {
            "label": chunk.label,
            "amount": s.MoneyOut.of(
                reports_mod.spend_total(session, user.id, chunk)
            ).model_dump(),
        }
        for chunk in split_into_weeks(DEMO_PERIOD)
    ]
    items = insights_mod.collect(session, user.id, report)
    questions = open_questions(session, user.id, limit=3)

    pct = None
    if report.prev_spend_total:
        pct = round(report.spend_delta / report.prev_spend_total * 100, 1)

    return {
        "offline": True,
        "period": {"label": DEMO_PERIOD.label, "days": DEMO_PERIOD.days},
        "spend": s.MoneyOut.of(report.spend_total).model_dump(),
        "income": s.MoneyOut.of(report.income_total).model_dump(),
        "avg_per_day": s.MoneyOut.of(report.avg_per_day).model_dump(),
        "prev_spend": s.MoneyOut.of(report.prev_spend_total).model_dump(),
        "spend_delta": s.MoneyOut.of(report.spend_delta, sign=True).model_dump(),
        "spend_delta_pct": pct,
        "awareness": s.awareness_out(report).model_dump(),
        "categories": [s.category_out(line).model_dump() for line in report.categories],
        "essential": s.MoneyOut.of(report.essential_total).model_dump(),
        "optional": s.MoneyOut.of(report.optional_total).model_dump(),
        "weeks": weeks,
        "questions": [
            json.loads(s.QuestionOut.of(item).model_dump_json()) for item in questions
        ],
        "open_questions": count_open_questions(session, user.id),
        "insights": [s.InsightOut.of(item).model_dump() for item in items],
        # Ровно то, что измерено. Раньше здесь стояли max(..., 12) и «или 1.4»,
        # и это было единственное показанное число, взятое не из данных —
        # в продукте, который весь построен на «цифрам можно верить».
        # Серия у демонстрационного пользователя настоящая: её создаёт
        # scripts/seed.py вместе с остальным демонстрационным набором.
        "streak": {
            "days": user.streak_days,
            "best": user.best_streak,
            "actions_per_day": round(average_actions_per_day(session, user.id), 2),
        },
        "operations_count": report.operations_count,
        "debts_owed_to_me": s.MoneyOut.of(report.debts_owed_to_me).model_dump(),
        "debts_i_owe": s.MoneyOut.of(report.debts_i_owe).model_dump(),
        # Уровень, задания, достижения и карта месяца — тем же движком и той
        # же формой ответа, что отдаёт GET /me/game. Руками этот блок писать
        # нельзя: прогресс партнёрских заданий и суммы станций считаются из
        # проводок, и любая рукопись разъехалась бы с живым API в первый же
        # день. model_dump_json заодно приводит даты к строкам, как в HTTP.
        "game": json.loads(
            state_out(game_state(session, user, DEMO_PERIOD)).model_dump_json()
        ),
    }


def check(payload: dict) -> list[str]:
    """Те же инварианты, что и в тестах: заглушка не имеет права им противоречить."""
    problems = []
    spend = payload["spend"]["kopecks"]
    weeks = sum(w["amount"]["kopecks"] for w in payload["weeks"])
    cats = sum(c["amount"]["kopecks"] for c in payload["categories"])
    split = payload["essential"]["kopecks"] + payload["optional"]["kopecks"]
    if weeks != spend:
        problems.append(f"недели {weeks} != месяц {spend}")
    if cats != spend:
        problems.append(f"категории {cats} != месяц {spend}")
    if split != spend:
        problems.append(f"обязательные+необязательные {split} != месяц {spend}")

    # Карта месяца — тот же месяц, что и всё остальное: сумма станций-недель
    # обязана сойтись с итогом. Если разъедется, экран сам себя опровергнет.
    game = payload["game"]
    stations = sum(
        st["amount"]["kopecks"]
        for st in game["map"]
        if st["kind"] == "week" and st["amount"]
    )
    if stations != spend:
        problems.append(f"станции-недели {stations} != месяц {spend}")
    if not game["quests"]:
        problems.append("заданий нет ни одного")
    if not game["achievements"]:
        problems.append("достижений нет ни одного")
    for quest in game["quests"]:
        if not 0.0 <= quest["progress"] <= 1.0:
            problems.append(f"задание {quest['code']}: прогресс {quest['progress']}")
    return problems


def main() -> None:
    with session_scope() as session:
        user = session.scalars(select(User).order_by(User.id)).first()
        if user is None:
            raise SystemExit("нет пользователей: сначала python -m scripts.seed --reset")
        payload = build_payload(session, user)

    problems = check(payload)
    if problems:
        raise SystemExit("данные не сходятся: " + "; ".join(problems))

    html = TARGET.read_text(encoding="utf-8")
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    replacement = f"const FALLBACK = {blob};\n"
    if not MARKER.search(html):
        raise SystemExit("в index.html не найден блок const FALLBACK = {...};")
    TARGET.write_text(MARKER.sub(replacement, html, count=1), encoding="utf-8")

    print(f"Обновил {TARGET.relative_to(ROOT)}")
    print(f"  период: {payload['period']['label']}")
    print(
        f"  потрачено: {payload['spend']['text']}, "
        f"осознанность {payload['awareness']['percent']}%"
    )
    print(f"  недель: {len(payload['weeks'])}, категорий: {len(payload['categories'])}, "
          f"вопросов: {len(payload['questions'])}, советов: {len(payload['insights'])}")
    level = payload["game"]["level"]
    earned = sum(1 for a in payload["game"]["achievements"] if a["earned"])
    print(
        f"  уровень {level['number']} «{level['title']}», "
        f"{level['xp']}/{level['xp_total_next']} XP; "
        f"заданий: {len(payload['game']['quests'])}, "
        f"достижений: {earned} из {len(payload['game']['achievements'])}, "
        f"станций: {len(payload['game']['map'])}"
    )
    print("  инварианты сошлись")


if __name__ == "__main__":
    main()
