"""Подсказки экономят открытие полного списка, но не скрывают категории."""

from types import SimpleNamespace
from unittest.mock import Mock

from app.category_recommendations import category_options, recommended_codes
from app.classify.enrich import category_from_answer


def _session(decisions=(), rules=(), operations=(), same_merchant=()):
    session = Mock()
    session.scalars.side_effect = [
        SimpleNamespace(all=lambda rows=rows: list(rows))
        for rows in (decisions, rules)
    ]
    session.execute.side_effect = [
        SimpleNamespace(all=lambda rows=rows: list(rows))
        for rows in (operations, same_merchant)
    ]
    return session


def _item(title="", counterparty_id=None):
    operation = SimpleNamespace(id=1, title=title, counterparty_id=counterparty_id)
    return SimpleNamespace(user_id=1, operation_id=1, operation=operation)


def test_four_recommendations_use_choice_frequency_and_text_context():
    options = category_options("unknown_place")
    decisions = [
        SimpleNamespace(anchor_hash=f"a-{index}", payload={"category": "sport"})
        for index in range(3)
    ]
    # Свободный ответ хранится двумя решениями, но считается одним выбором.
    decisions += [
        SimpleNamespace(anchor_hash="b", payload={"category": "beauty"}),
        SimpleNamespace(anchor_hash="b", payload={"category": "beauty"}),
    ]
    recommended = recommended_codes(
        _session(decisions=decisions), _item(title="Такси до дома"), options
    )
    assert recommended[:3] == ["taxi", "sport", "beauty"]
    assert len(recommended) == 4
    assert len(options) > 4
    assert "education" in {option["code"] for option in options}
    assert "other" not in {option["code"] for option in options}


def test_no_history_uses_common_categories():
    assert recommended_codes(
        _session(), _item(), category_options("category_unclear")
    ) == ["groceries", "cafe", "delivery", "taxi"]


def test_prior_choices_outweigh_automatic_frequency():
    decisions = [SimpleNamespace(anchor_hash="chosen", payload={"category": "education"})]
    recommended = recommended_codes(
        _session(decisions=decisions, operations=[("groceries", 100)]),
        _item(), category_options("unknown_place"),
    )
    assert recommended[0] == "education"


def test_answer_understands_category_names_in_plain_language():
    assert category_from_answer("купил продукты на неделю") == "groceries"
    assert category_from_answer("доставка еды") == "delivery"
    assert category_from_answer("новая одежда") == "clothes"
    assert category_from_answer("оплата за квартиру") == "housing"
