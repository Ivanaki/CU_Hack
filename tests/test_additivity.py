"""Сходимость периодов и целостность книги.

Кейс снимает баллы, если «суммы недельных срезов не складываются в
месячный». Здесь это проверяется на всех уровнях сразу: дни в недели,
недели в месяц, месяцы в квартал.
"""

from __future__ import annotations

from app.analytics import reports
from app.analytics.periods import (
    Period,
    month,
    split_into_days,
    split_into_months,
    split_into_weeks,
)
from app.ledger.invariants import run_all


def test_недели_складываются_в_месяц(session, seeded):
    september = month(2026, 9)
    total = reports.spend_total(session, seeded, september)
    by_weeks = sum(
        reports.spend_total(session, seeded, week) for week in split_into_weeks(september)
    )
    assert by_weeks == total


def test_дни_складываются_в_месяц(session, seeded):
    september = month(2026, 9)
    total = reports.spend_total(session, seeded, september)
    by_days = sum(
        reports.spend_total(session, seeded, day) for day in split_into_days(september)
    )
    assert by_days == total


def test_месяцы_складываются_в_квартал(session, seeded):
    from datetime import datetime

    quarter = Period(datetime(2026, 7, 1), datetime(2026, 10, 1), "III квартал")
    total = reports.spend_total(session, seeded, quarter)
    by_months = sum(
        reports.spend_total(session, seeded, chunk) for chunk in split_into_months(quarter)
    )
    assert by_months == total
    assert total > 0


def test_категории_складываются_в_общий_итог(session, seeded):
    september = month(2026, 9)
    report = reports.build_report(session, seeded, september)
    assert sum(line.amount for line in report.categories) == report.spend_total
    assert report.essential_total + report.optional_total == report.spend_total


def test_разбиение_на_недели_не_выходит_за_границы_месяца(session, seeded):
    september = month(2026, 9)
    weeks = split_into_weeks(september)
    assert weeks[0].start == september.start
    assert weeks[-1].end == september.end
    for earlier, later in zip(weeks, weeks[1:], strict=False):
        assert earlier.end == later.start, "между срезами не должно быть ни дыр, ни нахлёстов"


def test_книга_сходится(session, seeded):
    result = run_all(session, seeded)
    assert result.ok, result.problems


def test_пересчёт_идемпотентен(session, seeded):
    """Повторная классификация не меняет ни одной цифры."""
    from datetime import datetime

    from app.classify.pipeline import reclassify

    september = month(2026, 9)
    before = reports.build_report(session, seeded, september)
    reclassify(session, seeded, now=datetime(2026, 10, 10))
    after = reports.build_report(session, seeded, september)

    assert after.spend_total == before.spend_total
    assert after.income_total == before.income_total
    assert {line.code: line.amount for line in after.categories} == {
        line.code: line.amount for line in before.categories
    }
