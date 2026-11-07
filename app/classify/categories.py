"""Справочник категорий, MCC и ключевых слов мерчантов.

Категории несут флаг essential — обязательная трата или нет. Это закрывает
пункт кейса про разделение обязательных и необязательных трат и даёт
осмысленные советы: экономить предлагаем только там, где это возможно.
"""

from __future__ import annotations

from dataclasses import dataclass

OTHER = "other"
OTHER_INCOME = "other_income"
CASH_UNALLOCATED = "cash_unallocated"


@dataclass(frozen=True, slots=True)
class Category:
    code: str
    title: str
    emoji: str
    essential: bool = False
    is_income: bool = False


CATEGORIES: tuple[Category, ...] = (
    # --- расходы ---
    Category("groceries", "Продукты", "🛒", essential=True),
    Category("cafe", "Кафе и рестораны", "🍽"),
    Category("delivery", "Доставка еды", "🛵"),
    Category("taxi", "Такси", "🚕"),
    Category("transport", "Транспорт", "🚇", essential=True),
    Category("car", "Авто", "⛽"),
    Category("housing", "Жильё и ЖКХ", "🏠", essential=True),
    Category("connection", "Связь и интернет", "📶", essential=True),
    Category("health", "Здоровье", "💊", essential=True),
    Category("beauty", "Красота", "💇"),
    Category("clothes", "Одежда и обувь", "👕"),
    Category("electronics", "Техника и гаджеты", "💻"),
    Category("home", "Товары для дома", "🪑"),
    Category("entertainment", "Развлечения", "🎬"),
    Category("subscriptions", "Подписки", "🔁"),
    Category("education", "Образование", "📚"),
    Category("travel", "Путешествия", "✈️"),
    Category("sport", "Спорт", "🏋️"),
    Category("pets", "Питомцы", "🐾"),
    Category("gifts", "Подарки", "🎁"),
    Category("kids", "Дети", "🧸", essential=True),
    Category("fees", "Комиссии и проценты", "🏦", essential=True),
    Category("taxes", "Налоги", "🧾", essential=True),
    Category(CASH_UNALLOCATED, "Наличные (не распределены)", "💵"),
    Category(OTHER, "Прочее", "❔"),
    # --- доходы ---
    Category("salary", "Зарплата", "💼", is_income=True),
    Category("freelance", "Подработка", "🧑‍💻", is_income=True),
    Category("cashback", "Кэшбэк", "💳", is_income=True),
    Category("interest", "Проценты", "📈", is_income=True),
    Category("gift_income", "Подарок", "🎉", is_income=True),
    Category(OTHER_INCOME, "Прочий доход", "❔", is_income=True),
)

BY_CODE: dict[str, Category] = {c.code: c for c in CATEGORIES}
EXPENSE_CODES: tuple[str, ...] = tuple(c.code for c in CATEGORIES if not c.is_income)
INCOME_CODES: tuple[str, ...] = tuple(c.code for c in CATEGORIES if c.is_income)


def title(code: str | None) -> str:
    if not code:
        return "Без категории"
    cat = BY_CODE.get(code)
    return cat.title if cat else code


def label(code: str | None) -> str:
    """С эмодзи — для бота."""
    if not code:
        return "❔ Без категории"
    cat = BY_CODE.get(code)
    return f"{cat.emoji} {cat.title}" if cat else code


def is_essential(code: str | None) -> bool:
    cat = BY_CODE.get(code or "")
    return bool(cat and cat.essential)


def is_income_code(code: str | None) -> bool:
    cat = BY_CODE.get(code or "")
    return bool(cat and cat.is_income)


# --- MCC -> категория ------------------------------------------------------
# Только те коды, которые реально встречаются в выписках физлица.
MCC_CATEGORY: dict[str, str] = {
    # еда
    "5411": "groceries", "5422": "groceries", "5441": "groceries",
    "5451": "groceries", "5462": "groceries", "5499": "groceries",
    "5811": "cafe", "5812": "cafe", "5813": "cafe", "5814": "delivery",
    # транспорт
    "4121": "taxi", "4111": "transport", "4131": "transport", "4112": "transport",
    "4789": "transport",
    "5541": "car", "5542": "car", "7523": "car", "7538": "car", "7549": "car",
    "5533": "car",
    # путешествия
    "3000": "travel", "4511": "travel", "4722": "travel", "7011": "travel",
    "4411": "travel",
    # жильё и связь
    "4900": "housing", "6513": "housing",
    "4814": "connection", "4899": "connection", "4816": "connection",
    "5045": "electronics", "5732": "electronics", "5734": "electronics",
    "5722": "home", "5712": "home", "5200": "home", "5211": "home", "5251": "home",
    "5261": "home",
    # здоровье и красота
    "5912": "health", "8011": "health", "8021": "health", "8062": "health",
    "8042": "health", "8049": "health", "5975": "health", "5976": "health",
    "5977": "beauty", "7230": "beauty", "7297": "beauty", "7298": "beauty",
    # одежда
    "5651": "clothes", "5661": "clothes", "5691": "clothes", "5621": "clothes",
    "5611": "clothes", "5641": "clothes", "5699": "clothes", "5948": "clothes",
    # развлечения и подписки
    "7832": "entertainment", "7922": "entertainment", "7929": "entertainment",
    "7996": "entertainment", "7994": "entertainment", "7993": "entertainment",
    "5735": "entertainment", "5815": "subscriptions", "5816": "subscriptions",
    "5817": "subscriptions", "5818": "subscriptions", "4899_sub": "subscriptions",
    # образование и спорт
    "8220": "education", "8299": "education", "8211": "education", "5942": "education",
    "7997": "sport", "7941": "sport", "5941": "sport", "7991": "sport",
    # прочее
    "5995": "pets", "0742": "pets",
    "5947": "gifts", "5992": "gifts",
    "5641_kids": "kids", "5945": "kids",
    "9311": "taxes", "9222": "taxes", "9399": "taxes",
    "6012": "fees", "6051": "fees", "6010": "fees", "6011": "fees",
}

# MCC снятия наличных: обрабатываются отдельным правилом, не категорией.
MCC_ATM = frozenset({"6011", "6010"})

# --- ключевые слова мерчантов ---------------------------------------------
# Работает, когда MCC в выписке нет (частый случай у Сбера и в CSV-выгрузках).
MERCHANT_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("пятероч", "groceries"), ("перекрест", "groceries"), ("магнит", "groceries"),
    ("лента", "groceries"), ("ашан", "groceries"), ("вкусвилл", "groceries"),
    ("дикси", "groceries"), ("метро", "groceries"), ("окей", "groceries"),
    ("самокат", "groceries"), ("купер", "groceries"), ("сбермаркет", "groceries"),
    ("азбука вкуса", "groceries"), ("глобус", "groceries"), ("билла", "groceries"),
    ("yandex.lavka", "groceries"), ("лавка", "groceries"),
    ("яндекс еда", "delivery"), ("yandex.eda", "delivery"), ("delivery club", "delivery"),
    ("деливери", "delivery"), ("додо", "delivery"), ("kfc", "cafe"), ("бургер", "cafe"),
    ("вкусно и точка", "cafe"), ("макдон", "cafe"), ("старбакс", "cafe"),
    ("кофе", "cafe"), ("coffee", "cafe"), ("шоколадниц", "cafe"), ("теремок", "cafe"),
    ("сурф", "cafe"), ("ресторан", "cafe"), ("бар ", "cafe"), ("паб", "cafe"),
    ("яндекс go", "taxi"), ("yandex go", "taxi"), ("яндекс такси", "taxi"),
    ("ситимобил", "taxi"), ("uber", "taxi"), ("такси", "taxi"), ("didi", "taxi"),
    ("метрополит", "transport"), ("тройка", "transport"), ("мосгортранс", "transport"),
    ("ржд", "transport"), ("rzd", "transport"), ("аэроэкспресс", "transport"),
    ("подорожник", "transport"), ("транспортная карта", "transport"),
    ("лукойл", "car"), ("газпромнефть", "car"), ("роснефть", "car"), ("азс", "car"),
    ("татнефть", "car"), ("шелл", "car"), ("парковка", "car"), ("каршеринг", "car"),
    ("делимобиль", "car"), ("яндекс драйв", "car"),
    ("мтс", "connection"), ("билайн", "connection"), ("мегафон", "connection"),
    ("теле2", "connection"), ("tele2", "connection"), ("ростелеком", "connection"),
    ("жкх", "housing"), ("мосэнерго", "housing"), ("управляющая компания", "housing"),
    ("аренда квартиры", "housing"), ("наём", "housing"),
    ("аптек", "health"), ("здоров", "health"), ("клиник", "health"),
    ("инвитро", "health"), ("гемотест", "health"), ("стоматолог", "health"),
    ("медси", "health"), ("ригла", "health"), ("366", "health"),
    ("wildberries", "home"), ("ozon", "home"), ("яндекс маркет", "home"),
    ("днс", "electronics"), ("dns", "electronics"), ("мвидео", "electronics"),
    ("эльдорадо", "electronics"), ("citilink", "electronics"), ("ситилинк", "electronics"),
    ("restore", "electronics"), ("apple", "electronics"),
    ("леруа", "home"), ("икеа", "home"), ("ikea", "home"), ("hoff", "home"),
    ("askona", "home"), ("оби", "home"), ("фикс прайс", "home"),
    ("zara", "clothes"), ("uniqlo", "clothes"), ("h&m", "clothes"), ("спортмастер", "sport"),
    ("lamoda", "clothes"), ("befree", "clothes"), ("gloria", "clothes"),
    ("кинотеатр", "entertainment"), ("кино", "entertainment"), ("okko", "subscriptions"),
    ("ivi", "subscriptions"), ("кинопоиск", "subscriptions"), ("яндекс плюс", "subscriptions"),
    ("netflix", "subscriptions"), ("spotify", "subscriptions"), ("яндекс.плюс", "subscriptions"),
    ("vk музыка", "subscriptions"), ("сберпрайм", "subscriptions"), ("подписка", "subscriptions"),
    ("premium", "subscriptions"), ("icloud", "subscriptions"), ("google one", "subscriptions"),
    ("telegram premium", "subscriptions"), ("youtube", "subscriptions"),
    ("skyeng", "education"), ("skillbox", "education"), ("coursera", "education"),
    ("учебн", "education"), ("курс", "education"), ("литрес", "education"),
    ("фитнес", "sport"), ("world class", "sport"), ("бассейн", "sport"), ("ддх", "sport"),
    ("зоомаг", "pets"), ("четыре лапы", "pets"), ("ветеринар", "pets"),
    ("букет", "gifts"), ("цвет", "gifts"), ("подарк", "gifts"),
    ("детск", "kids"), ("детский мир", "kids"),
    ("комисси", "fees"), ("страхов", "fees"), ("госуслуг", "taxes"), ("налог", "taxes"),
    ("штраф", "taxes"), ("гибдд", "taxes"),
)

# Мерчанты, чья карта/кошелёк — отдельный счёт, а не трата.
# Пополнение такого счёта не является расходом; расход возникает при трате с него.
WALLET_MERCHANTS: tuple[str, ...] = (
    "wildberries", "wb кошел", "ozon card", "ozon карта", "озон карта",
    "яндекс пэй", "yandex pay", "сберспасибо", "халва",
)
