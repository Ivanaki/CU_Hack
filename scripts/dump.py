"""Диагностика: что система сделала с каждой операцией за период.

Нужна и в разработке, и на защите: показывает не только результат, но и
правило, по которому он получен.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.analytics.periods import month  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Operation, User, fmt_money  # noqa: E402


def main() -> None:
    # Схему подтягиваем сами: скрипт ходит в базу мимо приложения, и на
    # базе, снятой до добавления колонки (бэкап, копия тома, машина
    # коллеги), первый же SELECT упал бы трассой SQLAlchemy вместо дела.
    init_db()
    only = sys.argv[1] if len(sys.argv) > 1 else None
    period = month(2026, 9)
    with session_scope() as session:
        user = session.scalars(select(User)).first()
        ops = session.scalars(
            select(Operation)
            .where(
                Operation.user_id == user.id,
                Operation.occurred_at >= period.start,
                Operation.occurred_at < period.end,
            )
            .order_by(Operation.occurred_at)
        ).all()
        by_kind: dict[str, int] = {}
        for op in ops:
            by_kind[op.kind] = by_kind.get(op.kind, 0) + 1
        print("Операции по типам:")
        for kind, count in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            print(f"  {kind:<18} {count}")
        print()
        for op in ops:
            if only and only not in op.kind and only not in (op.rule or ""):
                continue
            print(
                f"{op.occurred_at:%d.%m %H:%M}  {op.kind:<16} "
                f"{fmt_money(op.amount):>12}  трата {fmt_money(op.spend_amount):>11}  "
                f"[{op.rule}] {op.title[:44]}"
            )


if __name__ == "__main__":
    main()
