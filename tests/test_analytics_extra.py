"""Периоды разной длины и разрезы вкладки «Отчёты».

Кейс называет четыре длины периода — день, неделя, месяц, год — и отдельным
пунктом снимает баллы за то, что срезы не складываются в целое. В
tests/test_additivity.py проверены дни, недели и месяцы внутри месяца и
квартала; здесь добавлен годовой уровень и всё, что считают новые разрезы:
продавцы, дни недели, средние чеки, подписки и сравнение по категориям.

Все проверки — свойства, а не заученные числа: разрез обязан складываться в
тот же итог, который показан на главном экране, каким бы этот итог ни был.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.analytics import reports
from app.analytics.periods import (
    Period,
    custom,
    day,
    month,
    parse_period,
    split_into_days,
    split_into_months,
    split_into_weeks,
    week,
    year,
)

YEAR = year(2026)
SEPTEMBER = month(2026, 9)
AUGUST = month(2026, 8)


# --------------------------------------------------------------------------
# Сходимость: годовой уровень
# --------------------------------------------------------------------------


def test_год_складывается_из_месяцев(session, seeded):
    total = reports.spend_total(session, seeded, YEAR)
    by_months = sum(
        reports.spend_total(session, seeded, chunk) for chunk in split_into_months(YEAR)
    )
    assert by_months == total
    assert total > 0, "в демонстрационном наборе должен быть хоть один год с тратами"


def test_год_складывается_из_недель(session, seeded):
    """Недели года пересекают границы месяцев — и всё равно сходятся.

    Это сложнее месячного случая: неделя 31 августа — 6 сентября лежит в
    двух месяцах, и обрезание по границе года не должно ни потерять её
    начало, ни посчитать его дважды.
    """
    total = reports.spend_total(session, seeded, YEAR)
    by_weeks = sum(
        reports.spend_total(session, seeded, chunk) for chunk in split_into_weeks(YEAR)
    )
    assert by_weeks == total


def test_год_складывается_из_дней(session, seeded):
    total = reports.spend_total(session, seeded, YEAR)
    days = split_into_days(YEAR)
    assert len(days) == 365
    assert sum(reports.spend_total(session, seeded, chunk) for chunk in days) == total


def test_неделя_складывается_из_дней(session, seeded):
    for chunk in split_into_weeks(SEPTEMBER):
        total = reports.spend_total(session, seeded, chunk)
        by_days = sum(
            reports.spend_total(session, seeded, part) for part in split_into_days(chunk)
        )
        assert by_days == total, f"неделя {chunk.label} не сходится по дням"


def test_все_четыре_длины_вложены_друг_в_друга(session, seeded):
    """День внутри недели, неделя внутри месяца, месяц внутри года."""
    busiest = max(
        split_into_days(SEPTEMBER),
        key=lambda part: reports.spend_total(session, seeded, part),
    )
    one_day = reports.spend_total(session, seeded, busiest)
    assert one_day > 0, "в сентябре должен быть день с тратами"

    its_week = week(busiest.start.date())
    amounts = [
        one_day,
        reports.spend_total(session, seeded, its_week),
        reports.spend_total(session, seeded, SEPTEMBER),
        reports.spend_total(session, seeded, YEAR),
    ]
    assert amounts == sorted(amounts), "более длинный период не может быть меньше"


def test_ключ_периода_понимается_для_всех_четырёх_длин():
    assert parse_period("2026") == YEAR
    assert parse_period("2026-09") == SEPTEMBER
    assert parse_period("2026-09-15") == day(datetime(2026, 9, 15).date())
    assert parse_period("2026-W38") == week(datetime(2026, 9, 15).date())
    with pytest.raises(ValueError):
        parse_period("позавчера")


def test_срезы_подбираются_по_длине_периода(session, seeded):
    """Неделя режется днями, месяц — неделями, год — месяцами."""
    expected = {
        "day": week(datetime(2026, 9, 15).date()),
        "week": SEPTEMBER,
        "month": YEAR,
    }
    for kind, window in expected.items():
        granularity, parts = reports.slice_breakdown(session, seeded, window)
        assert granularity == kind, f"{window.label}: ожидалась гранулярность {kind}"
        assert sum(amount for _, amount in parts) == reports.spend_total(
            session, seeded, window
        )
    _, months = reports.slice_breakdown(session, seeded, YEAR)
    assert len(months) == 12, "у года должно быть двенадцать столбиков, а не 53"


# --------------------------------------------------------------------------
# Разрезы
# --------------------------------------------------------------------------


def test_продавцы_складываются_в_итог_периода(session, seeded):
    """Топ продавцов — не выборка «самого заметного», а полное разложение."""
    merchants = reports.spend_by_merchant(session, seeded, SEPTEMBER)
    assert merchants
    assert sum(line.amount for line in merchants) == reports.spend_total(
        session, seeded, SEPTEMBER
    )
    assert merchants == sorted(merchants, key=lambda line: line.amount, reverse=True)


def test_у_продавца_считаются_покупки_и_средний_чек(session, seeded):
    merchants = reports.spend_by_merchant(session, seeded, SEPTEMBER)
    for line in merchants:
        assert line.name.strip()
        assert line.count >= 1
        assert line.average == line.amount // line.count
    assert sum(line.share for line in merchants) == pytest.approx(1.0, abs=1e-6)
    assert any(line.count > 1 for line in merchants), (
        "в месяце должен быть хоть один продавец, к которому ходили не один раз"
    )


def test_топ_обрезается_но_ничего_не_придумывает(session, seeded):
    merchants = reports.spend_by_merchant(session, seeded, SEPTEMBER)
    top = reports.top_merchants(session, seeded, SEPTEMBER, limit=3)
    assert top == merchants[:3]


def test_дни_недели_складываются_в_итог(session, seeded):
    lines = reports.spend_by_weekday(session, seeded, SEPTEMBER)
    assert [line.index for line in lines] == list(range(7)), "ось должна быть полной"
    assert sum(line.amount for line in lines) == reports.spend_total(
        session, seeded, SEPTEMBER
    )
    assert sum(line.days for line in lines) == SEPTEMBER.days
    for line in lines:
        assert line.avg_per_day == (line.amount // line.days if line.days else 0)


def test_самый_дорогой_день_недели_считается_по_среднему(session, seeded):
    """В месяце бывает пять пятниц и четыре субботы — сравнивать надо средние."""
    lines = reports.spend_by_weekday(session, seeded, SEPTEMBER)
    peak = reports.peak_weekday(lines)
    assert peak is not None
    assert peak.avg_per_day == max(line.avg_per_day for line in lines)
    assert peak.title in {
        "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье",
    }


def test_средний_чек_по_категориям_складывается_в_итог(session, seeded):
    lines = reports.average_check_by_category(session, seeded, SEPTEMBER)
    assert lines
    assert sum(line.amount for line in lines) == reports.spend_total(
        session, seeded, SEPTEMBER
    )
    by_category = reports.spend_by_category(session, seeded, SEPTEMBER)
    assert {line.code: line.amount for line in lines} == by_category
    for line in lines:
        assert line.count >= 1
        assert line.average == line.amount // line.count
        assert line.biggest >= line.average


def test_чек_это_операция_а_не_проводка(session, seeded):
    """Снятие наличных, разложенное по трём категориям, — три чека, но
    в каждой категории по одному, а не по числу проводок."""
    lines = reports.average_check_by_category(session, seeded, SEPTEMBER)
    operations = reports.build_report(session, seeded, SEPTEMBER).operations_count
    assert sum(line.count for line in lines) <= operations


def test_подписки_отдаются_списком_с_годовой_ценой(session, seeded):
    lines = reports.subscription_lines(session, seeded, SEPTEMBER)
    assert lines, "в демонстрационном наборе есть регулярные списания"
    for line in lines:
        assert line.label.strip()
        assert line.amount > 0
        assert line.yearly == line.amount * reports.charges_per_year(line.period_days)
        assert line.monthly == line.yearly // 12
        assert line.next_charge_at > line.last_charge_at
        assert line.occurrences >= 3, "подписка — это ритм, а не одно списание"
    monthly, yearly = reports.subscriptions_cost(lines)
    assert monthly == sum(line.monthly for line in lines)
    assert yearly == sum(line.yearly for line in lines)
    assert yearly > monthly


def test_месячная_подписка_стоит_в_год_двенадцать_списаний():
    """365/30 дало бы 12,17 списания в год — а их двенадцать."""
    assert reports.charges_per_year(30) == 12
    assert reports.charges_per_year(7) == 52
    assert reports.charges_per_year(365) == 1


def test_платежи_по_подписке_видны_по_проводкам(session, seeded):
    """Годовая цена — расчёт, а вот paid_in_period — факт из книги."""
    lines = reports.subscription_lines(session, seeded, SEPTEMBER)
    assert any(line.paid_in_period > 0 for line in lines)
    for line in lines:
        assert line.paid_in_period <= reports.spend_total(session, seeded, SEPTEMBER)


# --------------------------------------------------------------------------
# Сравнение с предыдущим периодом
# --------------------------------------------------------------------------


def test_месяц_сравнивается_с_календарным_прошлым_месяцем(session, seeded):
    """Сентябрь сравнивается с августом, а не с «2 августа – 1 сентября»."""
    assert SEPTEMBER.shifted_back() == AUGUST
    report = reports.build_report(session, seeded, SEPTEMBER)
    assert report.prev_spend_total == reports.spend_total(session, seeded, AUGUST)


def test_год_сравнивается_с_прошлым_годом():
    assert YEAR.shifted_back() == year(2025)


def test_произвольное_окно_сдвигается_на_свою_длину():
    window = custom(datetime(2026, 9, 3).date(), datetime(2026, 9, 12).date())
    previous = window.shifted_back()
    assert previous.end == window.start
    assert previous.days == window.days


def test_сравнение_по_категориям_видит_исчезнувшие(session, seeded):
    """Категория, которой в этом месяце нет, а в прошлом была, обязана
    попасть в сравнение: иначе падение на всю сумму никто не заметит."""
    changes = reports.category_changes(session, seeded, SEPTEMBER)
    now_map = reports.spend_by_category(session, seeded, SEPTEMBER)
    was_map = reports.spend_by_category(session, seeded, AUGUST)
    assert {line.code for line in changes} == set(now_map) | set(was_map)

    for line in changes:
        assert line.amount == now_map.get(line.code, 0)
        assert line.prev_amount == was_map.get(line.code, 0)
        assert line.delta == line.amount - line.prev_amount
    assert changes == sorted(changes, key=lambda line: abs(line.delta), reverse=True)


def test_направление_изменения_называется_честно(session, seeded):
    for line in reports.category_changes(session, seeded, SEPTEMBER):
        if line.direction == "new":
            assert line.prev_amount == 0 and line.amount
        elif line.direction == "gone":
            assert line.amount == 0 and line.prev_amount
        elif line.direction == "up":
            assert line.delta > 0
        elif line.direction == "down":
            assert line.delta < 0
        else:
            assert line.delta == 0


def test_выросшее_и_упавшее_вместе_дают_дельту_периода(session, seeded):
    changes = reports.category_changes(session, seeded, SEPTEMBER)
    delta = reports.spend_total(session, seeded, SEPTEMBER) - reports.spend_total(
        session, seeded, AUGUST
    )
    assert sum(line.delta for line in changes) == delta


# --------------------------------------------------------------------------
# Предъявление сходимости
# --------------------------------------------------------------------------


def test_проверки_сходимости_зелёные_на_месяце(session, seeded):
    checks = reports.additivity_checks(session, seeded, SEPTEMBER)
    names = {check.parts_name for check in checks}
    assert {"дни", "недели", "категории", "продавцы", "дни недели"} <= names
    for check in checks:
        assert check.ok, f"{check.title}: {check.parts} вместо {check.total}"
        assert check.title.endswith("в месяц")


def test_проверки_сходимости_зелёные_на_годе(session, seeded):
    checks = reports.additivity_checks(session, seeded, YEAR, with_days=True)
    names = {check.parts_name for check in checks}
    assert {"дни", "недели", "месяцы"} <= names
    for check in checks:
        assert check.ok, f"{check.title}: {check.parts} вместо {check.total}"
        assert check.title.endswith("в год")
    months = next(check for check in checks if check.parts_name == "месяцы")
    assert months.parts_count == 12


def test_длинный_период_по_умолчанию_не_считает_дни(session, seeded):
    """365 запросов ради строки в отчёте — плохая сделка; в /checkup они есть."""
    names = {
        check.parts_name for check in reports.additivity_checks(session, seeded, YEAR)
    }
    assert "дни" not in names
    assert "месяцы" in names


def test_проверка_сходимости_ловит_дыру_в_разбиении(session, seeded):
    """Если бы срезы теряли день, проверка обязана об этом сказать.

    Берём месяц без последнего дня: итог по «периоду» считается по полному
    месяцу, а части — по обрезанному, и сумма не сходится. Тест страхует
    саму проверку: зелёная строка, которая не умеет краснеть, бесполезна.
    """
    broken = Period(SEPTEMBER.start, datetime(2026, 9, 30), "сентябрь без 30-го")
    total = reports.spend_total(session, seeded, SEPTEMBER)
    parts = sum(
        reports.spend_total(session, seeded, chunk) for chunk in split_into_days(broken)
    )
    assert reports.AdditivityCheck("дни", "месяц", total, parts, 29).ok == (total == parts)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

ANALYTICS_TG_ID = 890


@pytest.fixture(scope="module")
def client(_isolated_db):
    """Свой пользователь: эти тесты только читают, но данные чужими не берут."""
    from tests.conftest import seed_user
    from tests.test_api import BOT_TOKEN

    seed_user(ANALYTICS_TG_ID)
    os.environ["BOT_TOKEN"] = BOT_TOKEN

    from app.config import get_settings

    get_settings.cache_clear()
    from app.api.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def auth() -> dict[str, str]:
    from tests.test_api import make_init_data

    return {"X-Telegram-Init-Data": make_init_data(ANALYTICS_TG_ID)}


def test_разрезы_отдаются_одним_запросом(client):
    response = client.get("/api/v1/me/analytics?period=2026-09", headers=auth())
    assert response.status_code == 200, response.text
    data = response.json()

    assert data["period"]["label"] == "Сентябрь 2026"
    assert data["prev_period"]["label"] == "Август 2026"
    assert data["granularity"] == "week"
    assert data["granularity_title"] == "По неделям"

    spend = data["spend"]["kopecks"]
    assert sum(part["amount"]["kopecks"] for part in data["slices"]) == spend
    assert sum(line["amount"]["kopecks"] for line in data["weekdays"]) == spend
    assert sum(line["amount"]["kopecks"] for line in data["average_checks"]) == spend
    assert data["top_merchants"], "продавцы должны быть"
    assert data["merchants_total"] >= len(data["top_merchants"])
    assert data["subscriptions"], "подписки в наборе есть"
    assert data["subscriptions_yearly"]["kopecks"] > data["subscriptions_monthly"]["kopecks"]
    assert data["additivity_ok"] is True
    assert all(check["ok"] for check in data["additivity"])


def test_разрезы_за_год_режутся_по_месяцам(client):
    data = client.get("/api/v1/me/analytics?period=2026", headers=auth()).json()
    assert data["granularity"] == "month"
    assert data["granularity_title"] == "По месяцам"
    assert len(data["slices"]) == 12
    assert sum(part["amount"]["kopecks"] for part in data["slices"]) == data["spend"]["kopecks"]
    assert data["prev_period"]["label"] == "2025"
    assert data["additivity_ok"] is True


def test_разрезы_за_неделю_режутся_по_дням(client):
    data = client.get("/api/v1/me/analytics?period=2026-W38", headers=auth()).json()
    assert data["granularity"] == "day"
    assert len(data["slices"]) == 7
    assert sum(part["amount"]["kopecks"] for part in data["slices"]) == data["spend"]["kopecks"]


def test_сводка_отдаёт_срезы_подходящей_длины(client):
    """У главного экрана появляется переключатель периодов — срезы обязаны
    приезжать готовыми, иначе на годе будет 53 столбика."""
    for spec, kind, count in (("2026-09", "week", 5), ("2026", "month", 12)):
        data = client.get(f"/api/v1/me/summary?period={spec}", headers=auth()).json()
        assert data["granularity"] == kind
        assert len(data["slices"]) == count
        assert sum(p["amount"]["kopecks"] for p in data["slices"]) == data["spend"]["kopecks"]
        assert sum(w["amount"]["kopecks"] for w in data["weeks"]) == data["spend"]["kopecks"]


def test_сводка_называет_период_сравнения(client):
    data = client.get("/api/v1/me/summary?period=2026-09", headers=auth()).json()
    assert data["prev_period"]["label"] == "Август 2026"
    assert data["prev_spend"]["kopecks"] > 0
    if data["forecast"]:
        assert data["forecast"]["period_label"] == "Сентябрь 2026"


def test_проверка_сходимости_показывает_все_уровни(client):
    data = client.get("/api/v1/me/checkup?period=2026-09", headers=auth()).json()
    assert data["ok"] is True
    assert data["weeks_sum_matches_month"] is True
    titles = {level["title"] for level in data["levels"]}
    assert "дни складываются в месяц" in titles
    assert "недели складываются в месяц" in titles
    assert "месяцы складываются в год" in titles
    assert all(level["ok"] for level in data["levels"])


def test_нераспознанный_период_это_понятная_ошибка(client):
    response = client.get("/api/v1/me/analytics?period=позавчера", headers=auth())
    assert response.status_code == 400
    assert "период" in response.json()["detail"]


def test_чужие_разрезы_не_видны(client):
    from tests.test_api import make_init_data

    data = client.get(
        "/api/v1/me/analytics?period=2026-09",
        headers={"X-Telegram-Init-Data": make_init_data(999_222)},
    ).json()
    assert data["spend"]["kopecks"] == 0
    assert data["top_merchants"] == []
    assert data["additivity_ok"] is True
