"""Короткая подпись участника перевода не должна выдавать реквизит за имя."""

from app.question_context import person_question_label


def test_person_question_label_prefers_real_name():
    context = {
        "counterparty": "Иван П.",
        "sources": [{"phone": "+79081112233"}],
    }
    assert person_question_label(context) == "Иван П."


def test_person_question_label_uses_phone_when_counterparty_is_technical():
    context = {
        "counterparty": "C172509261387692 быстрых от +",
        "sources": [{"phone": "+79081112233"}],
    }
    assert person_question_label(context) == "+79081112233"


def test_person_question_label_hides_unidentified_counterparty():
    assert person_question_label({"counterparty": "Договор 12345678", "sources": []}) is None
