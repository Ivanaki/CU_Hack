"""Чистит данные одного пользователя, не трогая остальных.

Нужно, когда демонстрационный набор мешает смотреть на свои настоящие
деньги: команда сносится только для указанного telegram id, чужие книги
остаются нетронутыми.

    python -m scripts.reset_user 1058389184          # показать, что удалится
    python -m scripts.reset_user 1058389184 --yes    # удалить
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, func, select  # noqa: E402

from app.db import init_db, session_scope  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    Claim,
    ClaimSettlement,
    Counterparty,
    DailyActivity,
    Decision,
    ImportBatch,
    MerchantRule,
    Operation,
    Posting,
    RawTransaction,
    ReviewItem,
    Subscription,
    User,
    fmt_money,
)


def main() -> None:
    # См. scripts/dump.py: скрипт работает с базой напрямую, поэтому сам
    # догоняет схему — иначе старая база падает на первом же запросе.
    init_db()
    parser = argparse.ArgumentParser()
    parser.add_argument("tg_id", type=int)
    parser.add_argument("--yes", action="store_true", help="действительно удалить")
    parser.add_argument(
        "--keep-account",
        action="store_true",
        default=True,
        help="оставить самого пользователя, снести только его данные",
    )
    args = parser.parse_args()

    with session_scope() as session:
        user = session.scalar(select(User).where(User.tg_id == args.tg_id))
        if user is None:
            raise SystemExit(f"пользователя {args.tg_id} нет")

        counts = {
            "операций": session.scalar(
                select(func.count()).select_from(Operation).where(Operation.user_id == user.id)
            ),
            "проводок": session.scalar(
                select(func.count()).select_from(Posting).where(Posting.user_id == user.id)
            ),
            "сырых строк": session.scalar(
                select(func.count())
                .select_from(RawTransaction)
                .where(RawTransaction.user_id == user.id)
            ),
            "вопросов": session.scalar(
                select(func.count()).select_from(ReviewItem).where(ReviewItem.user_id == user.id)
            ),
            "решений": session.scalar(
                select(func.count()).select_from(Decision).where(Decision.user_id == user.id)
            ),
        }
        spent = session.scalar(
            select(func.coalesce(func.sum(Posting.amount), 0)).where(
                Posting.user_id == user.id, Posting.account_kind == "expense"
            )
        )
        print(f"Пользователь {args.tg_id} ({user.display_name or 'без имени'}):")
        for name, value in counts.items():
            print(f"  {name:<14} {value}")
        print(f"  потрачено всего {fmt_money(int(spent or 0))}")

        if not args.yes:
            print("\nЭто предпросмотр. Добавьте --yes, чтобы удалить.")
            return

        # Порядок важен: сначала то, что ссылается, потом то, на что ссылаются.
        ops = select(Operation.id).where(Operation.user_id == user.id)
        session.execute(delete(ClaimSettlement).where(ClaimSettlement.operation_id.in_(ops)))
        session.execute(delete(Posting).where(Posting.user_id == user.id))
        session.execute(delete(ReviewItem).where(ReviewItem.user_id == user.id))
        session.execute(delete(Subscription).where(Subscription.user_id == user.id))
        session.execute(
            Operation.__table__.update()
            .where(Operation.user_id == user.id)
            .values(claim_id=None, parent_id=None)
        )
        session.execute(delete(Claim).where(Claim.user_id == user.id))
        session.execute(delete(RawTransaction).where(RawTransaction.user_id == user.id))
        session.execute(delete(Operation).where(Operation.user_id == user.id))
        session.execute(delete(ImportBatch).where(ImportBatch.user_id == user.id))
        session.execute(delete(Decision).where(Decision.user_id == user.id))
        session.execute(delete(DailyActivity).where(DailyActivity.user_id == user.id))
        session.execute(delete(MerchantRule).where(MerchantRule.user_id == user.id))

        # Счета остаются (пустые они никому не мешают и пересоздадутся сами),
        # но они ссылаются на контрагентов — снимаем ссылку, иначе удаление
        # контрагентов упирается во внешний ключ.
        session.execute(
            Account.__table__.update()
            .where(Account.user_id == user.id)
            .values(counterparty_id=None)
        )
        session.execute(delete(Counterparty).where(Counterparty.user_id == user.id))
        user.streak_days = 0
        user.best_streak = 0
        user.streak_last_date = None
        session.flush()
        print("\nГотово: данные снесены, аккаунт на месте. Загружайте свою выписку.")


if __name__ == "__main__":
    main()
