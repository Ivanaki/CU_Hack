"""Наполняет базу демонстрационным набором и печатает итоги за сентябрь.

Запускать так:
    python -m scripts.seed --reset
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.analytics import reports  # noqa: E402
from app.analytics.periods import month  # noqa: E402
from app.classify.pipeline import reclassify  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_engine, init_db, reset_state, session_scope  # noqa: E402
from app.ledger.invariants import run_all  # noqa: E402
from app.models import (  # noqa: E402
    Base,
    OperationSource,
    ReviewItem,
    ReviewStatus,
    User,
    fmt_money,
)

DEMO = ROOT / "data" / "demo"
DEMO_TG_ID = 1
DEMO_NAME = "Сергей К."


def ensure_user(session) -> User:
    user = session.scalar(select(User).where(User.tg_id == DEMO_TG_ID))
    if user is None:
        user = User(tg_id=DEMO_TG_ID, display_name=DEMO_NAME)
        session.add(user)
        session.flush()
    return user


def load_demo(session, user_id: int) -> None:
    files = [
        ("tbank_2026-07_09.csv", OperationSource.IMPORT),
        ("alfa_2026-09.csv", OperationSource.IMPORT),
        ("wildberries_2026-09.csv", OperationSource.IMPORT),
        ("cash_voice_2026-09.csv", OperationSource.VOICE),
    ]
    for name, origin in files:
        path = DEMO / name
        if not path.exists():
            print(f"  ! нет файла {name}, пропускаю")
            continue
        from app.ingest.base import parse_statement  # noqa: PLC0415
        from app.ingest.service import import_rows  # noqa: PLC0415

        bank, rows = parse_statement(path.read_bytes(), filename=name)
        result = import_rows(
            session, user_id, rows, source=bank, filename=name, origin=origin
        )
        print(f"  {name:<28} {result.summary}")


def seed_activity(session, user: User, *, days: int = 12) -> None:
    """Создаёт историю ежедневной активности демонстрационного пользователя.

    Это часть демонстрационного набора, такая же синтетическая, как и
    транзакции. Важно, что серия и среднее число действий потом СЧИТАЮТСЯ
    по этим записям, а не подставляются в момент показа: экран обязан
    показывать измеренное.
    """
    from datetime import date as _date
    from datetime import timedelta as _td

    from app.models import DailyActivity

    today = _date.today()
    for back in range(days):
        day = today - _td(days=back)
        row = session.scalar(
            select(DailyActivity).where(
                DailyActivity.user_id == user.id, DailyActivity.day == day
            )
        )
        if row is None:
            row = DailyActivity(user_id=user.id, day=day)
            session.add(row)
        # Один-два тапа в день — столько и требует инструмент.
        row.taps = 1 + (back % 2)
        row.imports = 1 if back in (0, days - 1) else 0
    user.streak_days = days
    user.best_streak = max(user.best_streak, days + 6)
    user.streak_last_date = today
    session.flush()


def show(session, user_id: int) -> None:
    september = month(2026, 9)
    report = reports.build_report(session, user_id, september)

    print()
    print("=" * 62)
    print(f"  ИТОГИ ЗА {september.label.upper()}")
    print("=" * 62)
    print(f"  Потрачено      {fmt_money(report.spend_total):>16}")
    print(f"  Получено       {fmt_money(report.income_total):>16}")
    print(f"  Разница        {fmt_money(report.net, sign=True):>16}")
    print(f"  В среднем в день {fmt_money(report.avg_per_day):>14}")
    prev = report.prev_spend_total
    if prev:
        print(f"  К августу      {fmt_money(report.spend_delta, sign=True):>16}")
    print()
    print(f"  Осознанность   {report.awareness.percent:>13} %")
    print(f"    не разобрано {fmt_money(report.awareness.unknown_volume):>16}")
    print(f"    без категории{fmt_money(report.awareness.uncategorized_volume):>16}")
    print(f"    наличные     {fmt_money(report.awareness.unallocated_cash):>16}")
    print()
    print("  По категориям:")
    for line in report.categories:
        mark = "•" if line.essential else " "
        delta = f"  ({fmt_money(line.delta, sign=True)})" if line.prev_amount else ""
        print(
            f"   {mark} {line.emoji} {line.title:<28} {fmt_money(line.amount):>14}"
            f"  {line.share * 100:4.1f}%{delta}"
        )
    print()
    print(f"  Обязательные   {fmt_money(report.essential_total):>16}")
    print(f"  Необязательные {fmt_money(report.optional_total):>16}")
    print()
    print("  Недельные срезы:")
    weeks = reports.weekly_breakdown(session, user_id, september)
    for period, total in weeks:
        print(f"    {period.label:<24} {fmt_money(total):>14}")
    weeks_sum = sum(total for _, total in weeks)
    status = "сходится" if weeks_sum == report.spend_total else "РАСХОЖДЕНИЕ"
    print(f"    {'сумма недель':<24} {fmt_money(weeks_sum):>14}   <- {status}")
    print()

    print(f"  Должны мне     {fmt_money(report.debts_owed_to_me):>16}")
    print(f"  Должен я       {fmt_money(report.debts_i_owe):>16}")
    print("  Остатки по счетам:")
    for name, balance in sorted(report.balances.items()):
        print(f"    {name:<26} {fmt_money(balance):>14}")

    questions = session.scalars(
        select(ReviewItem)
        .where(ReviewItem.user_id == user_id, ReviewItem.status == ReviewStatus.OPEN)
        .order_by(ReviewItem.priority.desc())
    ).all()
    print()
    print(f"  Вопросов в очереди: {len(questions)}")
    for item in questions[:8]:
        print(f"    — {item.question}")
        print(f"      {' / '.join(o['label'] for o in item.options)}")

    check = run_all(session, user_id)
    print()
    print(f"  Инварианты книги: {'все сошлись' if check.ok else 'ПРОБЛЕМЫ'}")
    for problem in check.problems[:10]:
        print(f"    ! {problem}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="снести базу и залить заново")
    args = parser.parse_args()

    if args.reset:
        # Файл сносим целиком: у claims и operations взаимные ссылки, и
        # построчное удаление ради чистого старта того не стоит.
        settings = get_settings()
        get_engine().dispose()
        reset_state()
        if settings.db_path and settings.db_path.exists():
            for suffix in ("", "-wal", "-shm"):
                candidate = settings.db_path.with_name(settings.db_path.name + suffix)
                candidate.unlink(missing_ok=True)
        else:
            Base.metadata.drop_all(get_engine())
    init_db()

    with session_scope() as session:
        user = ensure_user(session)
        print("Загрузка выписок:")
        load_demo(session, user.id)
        seed_activity(session, user)
        report = reclassify(session, user.id)
        print(
            f"\nКлассификация: операций {report.operations}, "
            f"вопросов {report.questions}, "
            f"не разобрано {fmt_money(report.unresolved_amount)}"
        )
        show(session, user.id)


if __name__ == "__main__":
    main()
