"""Генератор демонстрационного набора транзакций.

Набор детерминированный, а не случайный: он же служит тестовыми данными,
и на нём проверяется, что все семь сценариев кейса разбираются правильно.

Три месяца (июль–сентябрь 2026) и три источника нужны не для объёма:
  * три месяца — чтобы работало сравнение с предыдущим периодом и чтобы
    подписки обнаруживались по трём повторам, а не по объявлению;
  * три банка — чтобы переводы между своими картами были межбанковскими,
    как в сценарии 1, и чтобы кошелёк маркетплейса был отдельным счётом.

Все семь сценариев кейса живут в сентябре — это и есть месяц демонстрации.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "demo"

TBANK = "tbank"
ALFA = "alfa"
WB = "wb"

USER_NAME = "Сергей К."


@dataclass(slots=True)
class Row:
    when: str  # YYYY-MM-DD HH:MM
    account: str
    amount: int  # копейки, знаковые
    description: str
    mcc: str | None = None
    counterparty: str = ""
    category_hint: str = ""

    @property
    def dt(self) -> datetime:
        return datetime.strptime(self.when, "%Y-%m-%d %H:%M")


def rub(x: float) -> int:
    return int(round(x * 100))


# --------------------------------------------------------------------------
# Регулярный фон: то, из чего состоит обычный месяц человека с доходом ~300к
# --------------------------------------------------------------------------

MONTHS = ((2026, 7), (2026, 8), (2026, 9))


def background() -> list[Row]:
    """Фон месяца.

    Переменные траты — продукты, кафе, такси — от месяца к месяцу разные:
    в жизни они не повторяются копейка в копейку, а на одинаковых суммах
    детектор подписок начал бы принимать за подписку поход в магазин.
    Фиксированными остаются только те платежи, которые фиксированы и в
    реальности: аренда, связь, подписки.
    """
    rows: list[Row] = []
    for index, (year, month) in enumerate(MONTHS):
        m = f"{year}-{month:02d}"
        # Детерминированный, но разный по месяцам множитель.
        drift = (0.88, 1.0, 1.09)[index]

        # Доход
        rows += [
            Row(f"{m}-05 10:14", TBANK, rub(148_000), "ООО ТЕХНОЛАБ ЗАРПЛАТА", None,
                "ООО ТЕХНОЛАБ"),
            Row(f"{m}-20 10:09", TBANK, rub(152_000), "ООО ТЕХНОЛАБ ЗАРПЛАТА АВАНС", None,
                "ООО ТЕХНОЛАБ"),
            Row(f"{m}-28 12:00", TBANK, rub(1_240), "Кэшбэк за месяц", None, "Т-Банк"),
        ]

        # Обязательное
        rows += [
            Row(f"{m}-06 11:00", TBANK, -rub(72_000), "Аренда квартиры", None, "Смирнова Е. В."),
            Row(f"{m}-10 09:30", TBANK, -rub(4_800), "ЖКХ МОСЭНЕРГОСБЫТ", "4900",
                "МОСЭНЕРГОСБЫТ"),
            Row(f"{m}-12 19:05", TBANK, -rub(900), "МТС услуги связи", "4814", "МТС"),
        ]

        # Подписки: три повтора за три месяца — их и должен найти детектор
        rows += [
            Row(f"{m}-03 03:12", TBANK, -rub(399), "ЯНДЕКС ПЛЮС", "5817", "ЯНДЕКС ПЛЮС"),
            Row(f"{m}-15 04:20", TBANK, -rub(299), "VK MUSIC PODPISKA", "5817", "VK МУЗЫКА"),
            Row(f"{m}-22 05:31", TBANK, -rub(1_190), "APPLE ICLOUD STORAGE", "5817", "APPLE"),
        ]

        # Продукты
        for day, amount, shop in (
            (2, 3_240, "PYATEROCHKA 4312"),
            (7, 1_870, "VKUSVILL"),
            (11, 4_120, "PEREKRESTOK"),
            (16, 2_260, "VKUSVILL"),
            (21, 5_480, "LENTA HYPER"),
            (26, 1_540, "PYATEROCHKA 4312"),
        ):
            rows.append(
                Row(f"{m}-{day:02d} 19:{day + 10:02d}", TBANK,
                    -rub(round(amount * drift + day * 3, 2)), shop, "5411", shop)
            )

        # Кафе и доставка
        for day, amount, place, mcc in (
            (4, 890, "SURF COFFEE", "5812"),
            (9, 1_640, "YANDEX EDA", "5814"),
            (13, 2_310, "TEREMOK", "5812"),
            (18, 760, "SURF COFFEE", "5812"),
            (24, 3_180, "YANDEX EDA", "5814"),
        ):
            rows.append(
                Row(f"{m}-{day:02d} 13:{day:02d}", TBANK,
                    -rub(round(amount * drift + day, 2)), place, mcc, place)
            )

        # Такси — сознательно много: на этом строится совет по оптимизации
        for day, amount in ((1, 620), (3, 780), (8, 540), (12, 910), (17, 680),
                            (19, 1_240), (23, 590), (27, 830)):
            rows.append(
                Row(f"{m}-{day:02d} 09:{day:02d}", TBANK,
                    -rub(round(amount * drift + day * 2, 2)),
                    "YANDEX GO", "4121", "ЯНДЕКС GO")
            )

    return rows


# --------------------------------------------------------------------------
# Семь сценариев кейса. Сентябрь 2026.
# --------------------------------------------------------------------------


def scenarios() -> list[Row]:
    rows: list[Row] = []

    # Сценарий 1. Перевод 10 000 между своими картами разных банков.
    # В выписках это расход и доход, а по сути — ни то, ни другое.
    rows += [
        Row("2026-09-05 14:02", TBANK, -rub(10_000),
            f"Перевод себе на карту Альфа-Банк {USER_NAME}", None, USER_NAME),
        Row("2026-09-05 14:03", ALFA, rub(10_000),
            f"Перевод от {USER_NAME} Т-Банк", None, USER_NAME),
    ]

    # Сценарий 2. Пополнение карты маркетплейса 6000, затем траты с неё.
    # В выписке банка это перевод себе, в выписке WB — траты без категории.
    rows += [
        Row("2026-09-08 11:20", TBANK, -rub(6_000),
            "Пополнение Wildberries кошелёк", None, "WILDBERRIES"),
        Row("2026-09-08 11:21", WB, rub(6_000), "Пополнение с карты Т-Банк", None, USER_NAME),
        Row("2026-09-10 16:44", WB, -rub(2_300), "Продукты питания, заказ 8812", "5411",
            "WILDBERRIES"),
        Row("2026-09-12 18:10", WB, -rub(3_100), "Чайник электрический, заказ 8977", "5722",
            "WILDBERRIES"),
    ]

    # Сценарий 3. Долг 3000 и возврат двумя частями по 1500 с интервалом в неделю.
    rows += [
        Row("2026-09-12 20:15", TBANK, -rub(3_000), "Перевод Иван П. в долг до зарплаты",
            None, "Иван П."),
        Row("2026-09-19 12:40", TBANK, rub(1_500), "Перевод от Иван П. возврат части долга",
            None, "Иван П."),
        Row("2026-09-26 18:55", TBANK, rub(1_500), "Перевод от Иван П. отдаю остаток",
            None, "Иван П."),
    ]

    # Сценарий 4. Счёт 8000 за пять человек, четыре возврата по 1600.
    # Реальная трата — одна пятая.
    rows += [
        Row("2026-09-14 21:30", TBANK, -rub(8_000), "WHITE RABBIT RESTORAN", "5812",
            "WHITE RABBIT"),
        Row("2026-09-15 10:05", TBANK, rub(1_600), "Перевод от Анна М. за ужин", None,
            "Анна М."),
        Row("2026-09-15 22:41", TBANK, rub(1_600), "Перевод от Дмитрий Л. за ужин", None,
            "Дмитрий Л."),
        Row("2026-09-16 09:18", TBANK, rub(1_600), "Перевод от Павел С. за ужин", None,
            "Павел С."),
        Row("2026-09-18 19:02", TBANK, rub(1_600), "Перевод от Ольга Т. за ужин", None,
            "Ольга Т."),
    ]

    # Сверх списка кейса: долг в обратную сторону — заняли мне, я вернул.
    # Ни поступление, ни возврат не должны попасть в доходы и расходы.
    rows += [
        Row("2026-09-07 19:40", TBANK, rub(7_000),
            "Перевод от Максим В. даю в долг до зарплаты", None, "Максим В."),
        Row("2026-09-21 11:15", TBANK, -rub(7_000),
            "Перевод Максим В. возвращаю долг", None, "Максим В."),
    ]

    # Сценарий 5. Поступление 500 "за обед" при отсутствии зафиксированного долга.
    # Правильный ответ — не гадать, а спросить.
    rows.append(
        Row("2026-09-22 13:37", TBANK, rub(500), "Перевод от Катя Н. за обед", None, "Катя Н.")
    )

    # Сценарий 6. Возврат товара: 4200 приходят обратно. Доходом не являются.
    rows += [
        Row("2026-09-03 17:22", TBANK, -rub(4_200), "DNS SHOP", "5732", "DNS SHOP"),
        Row("2026-09-17 15:10", TBANK, rub(4_200), "DNS SHOP возврат покупки", "5732",
            "DNS SHOP"),
    ]

    # Сценарий 7. Снятие 5000 в банкомате. Часть объяснена голосом, часть — нет.
    rows += [
        Row("2026-09-09 08:50", TBANK, -rub(5_000), "Снятие наличных ATM Т-Банк", "6011",
            "ATM"),
    ]

    return rows


def cash_entries() -> list[Row]:
    """Голосовые заметки о тратах наличными.

    Из снятых 5000 объяснено 2000. Остальные 3000 через две недели уйдут в
    отдельную строку "наличные без назначения" — инструмент не делает вид,
    что знает, куда они делись.
    """
    return [
        Row("2026-09-09 12:30", "cash", -rub(1_200), "Рынок, овощи и фрукты", None, "Рынок"),
        Row("2026-09-11 18:05", "cash", -rub(800), "Кофе с собой у дома", None, "Кофейня"),
    ]


# --------------------------------------------------------------------------
# Запись файлов в форматах конкретных банков
# --------------------------------------------------------------------------


def write_tbank(rows: list[Row], path: Path) -> None:
    """Настоящий формат выписки Т-Банка: cp1251, точка с запятой, запятая в числе."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow([
        "Дата операции", "Дата платежа", "Номер карты", "Статус", "Сумма операции",
        "Валюта операции", "Сумма платежа", "Валюта платежа", "Кэшбэк", "Категория",
        "MCC", "Описание", "Бонусы (включая кэшбэк)",
    ])
    for row in rows:
        amount = f"{row.amount / 100:.2f}".replace(".", ",")
        writer.writerow([
            row.dt.strftime("%d.%m.%Y %H:%M:%S"),
            row.dt.strftime("%d.%m.%Y"),
            "*4127", "OK", amount, "RUB", amount, "RUB", "0,00",
            row.category_hint, row.mcc or "", row.description, "0,00",
        ])
    path.write_bytes(buf.getvalue().encode("cp1251", errors="replace"))


def write_alfa(rows: list[Row], path: Path) -> None:
    """Формат Альфа-Банка: utf-8 с BOM, назначение платежа отдельной колонкой."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(["Дата операции", "Номер счета", "Сумма", "Валюта", "Назначение платежа"])
    for row in rows:
        writer.writerow([
            row.dt.strftime("%d.%m.%Y"),
            "40817810900000005531",
            f"{row.amount / 100:.2f}".replace(".", ","),
            "RUB",
            row.description,
        ])
    path.write_bytes(buf.getvalue().encode("utf-8-sig"))


def write_canonical(rows: list[Row], path: Path, bank: str) -> None:
    """Наш формат: то, во что превращается любая выписка после разбора."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        ["datetime", "bank", "card", "amount", "currency", "mcc", "counterparty", "description"]
    )
    for row in rows:
        writer.writerow([
            row.dt.strftime("%Y-%m-%d %H:%M:%S"), bank, "", f"{row.amount / 100:.2f}",
            "RUB", row.mcc or "", row.counterparty, row.description,
        ])
    path.write_bytes(buf.getvalue().encode("utf-8-sig"))


def build() -> dict[str, Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    everything = background() + scenarios()
    everything.sort(key=lambda r: r.dt)

    by_account: dict[str, list[Row]] = {}
    for row in everything:
        by_account.setdefault(row.account, []).append(row)

    files: dict[str, Path] = {}

    tbank_path = OUT / "tbank_2026-07_09.csv"
    write_tbank(by_account.get(TBANK, []), tbank_path)
    files["tbank"] = tbank_path

    alfa_path = OUT / "alfa_2026-09.csv"
    write_alfa(by_account.get(ALFA, []), alfa_path)
    files["alfa"] = alfa_path

    wb_path = OUT / "wildberries_2026-09.csv"
    write_canonical(by_account.get(WB, []), wb_path, "Wildberries")
    files["wb"] = wb_path

    cash_path = OUT / "cash_voice_2026-09.csv"
    write_canonical(cash_entries(), cash_path, "Наличные")
    files["cash"] = cash_path

    return files


if __name__ == "__main__":
    created = build()
    total = len(background() + scenarios() + cash_entries())
    print(f"Демонстрационный набор: {total} операций")
    for name, path in created.items():
        rows = sum(1 for _ in path.read_bytes().splitlines()) - 1
        print(f"  {name:<8} {path.relative_to(ROOT)}  ({rows} строк)")
