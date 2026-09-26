"""Явные номера в переводах: свой, контакт и опасные ложные совпадения."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.schemas import QuestionOut
from app.classify.phone_identities import (
    PhoneRejected,
    delete_identity,
    normalize_phone,
    save_identity,
)
from app.classify.pipeline import reclassify
from app.ingest.base import ParsedRow
from app.ingest.service import import_rows
from app.models import Operation, OperationKind, PhoneIdentity, ReviewItem, User
from app.service import answer_question
from tests.test_api import BOT_TOKEN, make_init_data


@pytest.fixture(autouse=True)
def _db(_isolated_db):
    from app.db import init_db
    init_db()


def user(session, tg_id):
    person = User(tg_id=tg_id, display_name="Алексей")
    session.add(person)
    session.flush()
    return person


def ops(session, person):
    return list(session.scalars(select(Operation).where(Operation.user_id == person.id)).all())


def pair(
    session, person, *, outgoing="Перевод по номеру телефона",
    incoming="Поступление от +7 900 000-00-01",
):
    import_rows(session, person.id, [ParsedRow(
        booked_at=datetime(2026, 9, 25, 10), amount=-1_000_424,
        description=outgoing, bank="Т-Банк", last4="1001",
    )], source="Т-Банк")
    import_rows(session, person.id, [ParsedRow(
        booked_at=datetime(2026, 9, 25, 10, 5), amount=1_000_424,
        description=incoming, bank="Альфа-Банк", last4="2002",
    )], source="Альфа-Банк")


def test_adding_own_number_merges_two_existing_bank_rows(session):
    person = user(session, 981001)
    pair(session, person)
    reclassify(session, person.id)
    assert len(ops(session, person)) == 2
    save_identity(session, person.id, "8 (900) 000-00-01", "own")
    reclassify(session, person.id)
    result = ops(session, person)
    assert len(result) == 1
    assert result[0].kind == OperationKind.TRANSFER
    assert result[0].rule == "self_transfer"
    assert len(result[0].legs) == 2
    assert result[0].spend_amount == 0
    assert not [p for p in result[0].postings if p.account_kind in ("expense", "income")]
    assert not session.scalars(select(ReviewItem).where(ReviewItem.user_id == person.id)).all()
    assert delete_identity(session, person.id, "+79000000001")
    reclassify(session, person.id)
    assert len(ops(session, person)) == 2


def test_unknown_different_numbers_do_not_merge(session):
    person = user(session, 981002)
    pair(session, person,
         outgoing="Перевод на +7 900 000-00-02",
         incoming="Поступление от +7 900 000-00-01")
    save_identity(session, person.id, "+79000000001", "own")
    reclassify(session, person.id)
    assert len(ops(session, person)) == 2
    assert not any(op.rule == "self_transfer" for op in ops(session, person))


def test_contact_outgoing_conflicts_with_own_incoming(session):
    person = user(session, 981003)
    pair(session, person,
         outgoing="Перевод Петр С. на +7 900 000-00-02",
         incoming="Поступление от +7 900 000-00-01")
    save_identity(session, person.id, "+79000000001", "own")
    save_identity(session, person.id, "+79000000002", "contact", "Петя")
    reclassify(session, person.id)
    assert len(ops(session, person)) == 2
    assert any("Петя" in op.title for op in ops(session, person))


def test_shared_surname_does_not_merge_transfer_to_another_person(session):
    person = user(session, 981013)
    person.display_name = "Алексей Иванов"
    pair(session, person,
         outgoing="Перевод на +7 900 000-00-01",
         incoming="Поступление от Петр Иванов")
    save_identity(session, person.id, "+79000000001", "own")
    reclassify(session, person.id)
    assert len(ops(session, person)) == 2
    assert not any(op.rule == "self_transfer" for op in ops(session, person))


def test_contact_name_on_incoming_question_and_rename(session):
    person = user(session, 981004)
    import_rows(session, person.id, [ParsedRow(
        booked_at=datetime(2026, 9, 25, 10), amount=50_000,
        description="Перевод от +7 900 000-00-03", bank="Альфа-Банк", last4="2003",
    )], source="Альфа-Банк")
    save_identity(session, person.id, "+79000000003", "contact", "Петя")
    reclassify(session, person.id)
    question = session.scalar(select(ReviewItem).where(ReviewItem.user_id == person.id))
    assert QuestionOut.of(question).person_label == "Петя"
    save_identity(session, person.id, "+79000000003", "contact", "Пётр")
    reclassify(session, person.id)
    question = session.scalar(select(ReviewItem).where(ReviewItem.user_id == person.id))
    assert QuestionOut.of(question).person_label == "Пётр"


def test_contact_name_on_future_outgoing_transfer(session):
    person = user(session, 981010)
    save_identity(session, person.id, "+79000000007", "contact", "Петя")
    import_rows(session, person.id, [ParsedRow(
        booked_at=datetime(2026, 9, 26, 10), amount=-70_000,
        description="Перевод на +7 900 000-00-07", bank="Т-Банк", last4="1007",
    )], source="Т-Банк")
    reclassify(session, person.id)
    question = session.scalar(select(ReviewItem).where(ReviewItem.user_id == person.id))
    assert QuestionOut.of(question).person_label == "Петя"
    assert ops(session, person)[0].kind == OperationKind.UNKNOWN


def test_own_number_without_other_bank_is_neither_income_nor_expense(session):
    person = user(session, 981011)
    import_rows(session, person.id, [ParsedRow(
        booked_at=datetime(2026, 9, 26, 10), amount=40_000,
        description="Поступление от +7 900 000-00-08", bank="Альфа-Банк", last4="2008",
    )], source="Альфа-Банк")
    save_identity(session, person.id, "+79000000008", "own")
    reclassify(session, person.id)
    result = ops(session, person)
    assert len(result) == 1
    assert result[0].kind == OperationKind.TRANSFER
    assert result[0].rule == "own_accounts_by_text"
    assert not [p for p in result[0].postings if p.account_kind in ("expense", "income")]


def test_free_text_self_transfer_does_not_become_income(session):
    person = user(session, 981012)
    import_rows(session, person.id, [ParsedRow(
        booked_at=datetime(2026, 9, 26, 10), amount=40_000,
        description="Перевод от +7 900 000-00-09", bank="Альфа-Банк", last4="2009",
    )], source="Альфа-Банк")
    reclassify(session, person.id)
    question = session.scalar(select(ReviewItem).where(ReviewItem.user_id == person.id))
    assert question is not None
    answer_question(session, person, question.id, "free", "перевёл себе на другой банк")
    result = ops(session, person)
    assert len(result) == 1
    assert result[0].kind == OperationKind.TRANSFER
    assert result[0].spend_amount == 0
    assert not [p for p in result[0].postings if p.account_kind in ("expense", "income")]


def test_own_number_isolated_by_user_and_not_mobile_payment(session):
    owner = user(session, 981005)
    stranger = user(session, 981006)
    save_identity(session, owner.id, "+79000000004", "own")
    for person in (owner, stranger):
        import_rows(session, person.id, [ParsedRow(
            booked_at=datetime(2026, 9, 25, 11), amount=-30_000,
            description="Оплата связи на +7 900 000-00-04", bank="Т-Банк", last4="1004",
        )], source="Т-Банк")
        reclassify(session, person.id)
        assert not any(op.kind == OperationKind.TRANSFER for op in ops(session, person))
    assert session.scalar(select(PhoneIdentity).where(PhoneIdentity.user_id == stranger.id)) is None


def test_phone_validation_and_conflicting_roles(session):
    person = user(session, 981007)
    assert normalize_phone("8 (900) 000-00-05") == "+79000000005"
    for bad in ("0005", "1234567890", "+790000000051", "abc+79000000005"):
        with pytest.raises(PhoneRejected):
            normalize_phone(bad)
    save_identity(session, person.id, "+79000000005", "own")
    with pytest.raises(PhoneRejected):
        save_identity(session, person.id, "+79000000005", "contact", "Петя")


def test_phone_api_is_private_and_delete_all_removes_rules(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", BOT_TOKEN)
    from app.api.app import create_app
    from app.config import get_settings

    get_settings.cache_clear()
    first = {"X-Telegram-Init-Data": make_init_data(981008, token=BOT_TOKEN)}
    second = {"X-Telegram-Init-Data": make_init_data(981009, token=BOT_TOKEN)}
    with TestClient(create_app()) as client:
        saved = client.post(
            "/api/v1/me/phone-identities",
            headers=first,
            json={"phone": "8 (900) 000-00-06", "kind": "contact", "name": "Петя"},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json() == {"phone": "+79000000006", "kind": "contact", "name": "Петя"}
        assert client.get("/api/v1/me/phone-identities", headers=second).json() == []
        assert len(client.get("/api/v1/me/phone-identities", headers=first).json()) == 1
        assert client.delete("/api/v1/me/data", headers=first).status_code == 200
        assert client.get("/api/v1/me/phone-identities", headers=first).json() == []
    get_settings.cache_clear()
