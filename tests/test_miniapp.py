"""Mini App как единый файл: инструкция по запуску двойным касанием.

Экран отдаётся статикой тем же процессом, что и API, сборщиков и тестов на
JS в проекте нет — значит ошибка в index.html обнаруживается только глазами
на демонстрации. Здесь проверяется то, что ломается молча: дословность
инструкции (её согласовал заказчик), адрес быстрой команды, безопасность
внешней ссылки, живость обработчиков и то, что scripts.build_miniapp
по-прежнему найдёт в файле блок запасных данных.

Тексты сверяются не с сырой разметкой, а с видимым текстом инструкции: так
перенос строки, лишний отступ или неразрывный пробел в вёрстке не ломают
проверку, а подмена слов — ломает.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "miniapp" / "dist" / "index.html"

SHORTCUT_URL = "https://www.icloud.com/shortcuts/c7bc0cc38a5d430fbbd609ab2d6dddbf"

HTML = INDEX.read_text(encoding="utf-8")
FLAT = re.sub(r"\s+", " ", HTML)

VOID_TAGS = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)

# Тексты согласованы с заказчиком и проверяются дословно: «примерно те же
# слова» здесь не годятся — по ним человек ищет пункты в чужом интерфейсе.
STEP_TITLES = ("Установите команду", "Назначьте двойное касание", "Проверьте работу")

STEP_LINES = (
    "Нажмите кнопку «Установить команду» ниже.",
    "На открывшейся странице нажмите «Получить быструю команду» или «Добавить команду».",
    "Откроется приложение «Команды».",
    "Нажмите «Добавить быструю команду».",
    "Откройте «Настройки» на iPhone.",
    "Выберите «Универсальный доступ».",
    "Откройте раздел «Касание».",
    "Пролистайте вниз и нажмите «Коснуться сзади».",
    "Выберите «Двойное касание».",
    "Пролистайте до раздела «Команды».",
    "Выберите только что добавленную команду.",
)

TROUBLE_LINES = (
    "Откройте приложение «Команды» и один раз запустите команду вручную.",
    "Разрешите доступ, если iPhone его запросит.",
    "Попробуйте сделать два более чётких касания.",
    "Толстый чехол может мешать распознаванию.",
    "Функция доступна на iPhone 8 и более новых моделях.",
)

# Пункт 9 ТЗ — запрет: приложение не должно обещать, что само настроит iPhone.
# Проверяем отсутствие таких обещаний, а не наличие дисклеймера: страница с
# дисклеймером и обещанием одновременно тест бы прошла.
FORBIDDEN_CLAIMS = (
    "настроим за вас",
    "настроим ваш",
    "настроит за вас",
    "приложение настроит",
    "автоматически назнач",
    "само назнач",
    "сами назначим",
    "приложение изменит",
    "изменим настройки",
    "изменит настройки",
)

# Идентификаторы, за которые скрипт держится руками.
WIRED_IDS = ("howtoOpen", "howtoDone", "howtoClose", "howtoInstall")


class Markup(HTMLParser):
    """Минимальный разбор: своих зависимостей у тестов HTML быть не должно."""

    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.tags: list[str] = []
        self.links: list[dict[str, str]] = []
        self.external: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: (v or "") for k, v in attrs}
        self.tags.append(tag)
        if attr.get("id"):
            self.ids.append(attr["id"])
        if tag == "a" and attr.get("href"):
            self.links.append(attr)
        src = attr.get("src") if tag in {"script", "iframe", "img"} else None
        if tag == "link":
            src = attr.get("href")
        if src and src.startswith(("http://", "https://", "//")):
            self.external.append(src)


class Section(HTMLParser):
    """Видимый текст одного узла по id — то, что человек действительно читает."""

    def __init__(self, target: str) -> None:
        super().__init__()
        self.target = target
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.depth:
            if tag not in VOID_TAGS:
                self.depth += 1
        elif dict(attrs).get("id") == self.target:
            self.depth = 1

    def handle_endtag(self, tag: str) -> None:
        if self.depth and tag not in VOID_TAGS:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.depth:
            self.parts.append(data)


def markup() -> Markup:
    parsed = Markup()
    parsed.feed(HTML)
    return parsed


def section_text(target: str) -> str:
    parsed = Section(target)
    parsed.feed(HTML)
    assert parsed.parts, f"узел #{target} не найден"
    # Неразрывные пробелы в блоке-пути тоже сводим к обычным: это пробелы.
    return re.sub(r"\s+", " ", "".join(parsed.parts)).strip()


TEXT = section_text("howto")


def test_маркер_запасных_данных_на_месте_и_один():
    """Регулярка сборки и файл обязаны совпадать, иначе запасные данные устареют молча."""
    from scripts.build_miniapp import MARKER  # импорт внутри теста: бэкенд тут ни при чём

    assert len(MARKER.findall(HTML)) == 1, (
        "scripts.build_miniapp перезаписывает ровно один блок запасных данных"
    )


def test_инструкция_живёт_в_разметке_а_не_в_запасных_данных():
    """Сборка перезапишет блок запасных данных — текст инструкции не должен в нём оказаться."""
    fallback = re.search(r"const FALLBACK = (\{.*?\});\n", HTML, re.DOTALL).group(1)
    assert "Настройте запуск двойным касанием" not in fallback


def test_заголовок_и_обещание_одной_настройки():
    assert "Настройте запуск двойным касанием" in TEXT
    assert (
        "Это нужно сделать только один раз. Настройка займёт примерно 1–2 минуты." in TEXT
    ), "человек должен заранее знать, во что ввязывается"


def test_все_три_этапа_на_месте_и_пронумерованы():
    for title in STEP_TITLES:
        assert title in TEXT, f"этап «{title}» пропал"
    for number in ("1", "2", "3"):
        assert f'<span class="step-n">{number}</span>' in HTML, f"этап {number} без номера"


def test_шаги_этапов_приведены_дословно():
    missing = [line for line in STEP_LINES if line not in TEXT]
    assert not missing, f"шаги разошлись с согласованным текстом: {missing}"


def test_проверка_работы_описана_дословно():
    """Единственный абзац без списка — в разметке он разбит переносом строк."""
    assert (
        "Дважды постучите пальцем по задней панели iPhone в области логотипа Apple. "
        "Команда должна запуститься." in TEXT
    )


def test_путь_в_настройках_показан_одной_строкой():
    path = (
        "Настройки → Универсальный доступ → Касание → Коснуться сзади → "
        "Двойное касание → нужная команда"
    )
    assert path in TEXT


def test_названия_пунктов_настроек_не_двоятся():
    """Два имени одного пункта в одной карточке — человек не знает, что искать."""
    assert "Коснуться сзади" in TEXT
    for alias in ("Касание задней панели", "Быстрые команды"):
        assert alias not in TEXT, (
            f"«{alias}» противоречит согласованному тексту в той же карточке"
        )


def test_блок_не_получилось_сворачивается_и_полон():
    assert "<summary>Не получилось?</summary>" in HTML, "подсказки свёрнуты по умолчанию"
    missing = [line for line in TROUBLE_LINES if line not in TEXT]
    assert not missing, f"пропали подсказки: {missing}"


def test_кнопка_установки_ведёт_на_нужную_команду():
    installs = [a for a in markup().links if a.get("href") == SHORTCUT_URL]
    assert len(installs) == 1, "кнопка «Установить команду» одна и ведёт на согласованный адрес"
    assert installs[0].get("target") == "_blank"
    assert installs[0].get("rel") == "noopener noreferrer", (
        "без noopener открытая вкладка получает доступ к window.opener"
    )
    assert ">Установить команду</a>" in FLAT, "надпись на основной кнопке задана ТЗ"


def test_ссылка_перехватывается_только_внутри_telegram():
    """SDK подключён безусловно, поэтому openLink есть и в обычном браузере.

    Если опираться на наличие метода, preventDefault убивает тот самый
    запасной переход по href, ради которого он и написан.
    """
    guard = re.search(r"const inTelegram = ([^;]+);", HTML)
    assert guard, "нужна явная проверка, что мы внутри клиента Telegram"
    assert "platform" in guard.group(1) and "initData" in guard.group(1), (
        "клиент отличается от браузера по platform/initData, а не по наличию openLink"
    )
    body = re.search(r"function openShortcutLink\(event\) \{(.+?)\n\}", HTML, re.DOTALL)
    assert body, "обработчик кнопки установки пропал"
    assert "inTelegram" in body.group(1), "вне Telegram клик перехватывать нечем и незачем"


def test_страница_icloud_не_встраивается():
    assert "iframe" not in markup().tags, "iCloud не встраивается, и доверия это не добавляет"


def test_приложение_не_обещает_менять_настройки_айфона():
    """Пункт 9 ТЗ — запрет, поэтому проверяем отсутствие обещаний, а не дисклеймер."""
    lowered = TEXT.lower()
    found = [claim for claim in FORBIDDEN_CLAIMS if claim in lowered]
    assert not found, f"назначение команды делает человек руками, а текст обещает: {found}"


def test_инструкция_закрывается_и_возвращает_назад():
    assert ">Понятно<" in HTML, "нужна кнопка выхода из инструкции"
    assert "'Escape'" in HTML, "закрытие по Esc"
    assert "window.scrollTo(0, howtoScrollY)" in HTML, "возврат ровно туда, где человек был"


def test_обработчики_не_роняют_страницу():
    """Скрипт инлайновый: TypeError на верхнем уровне оставляет экран пустым целиком."""
    ids = markup().ids
    for wanted in (*WIRED_IDS, "howto", "app"):
        assert ids.count(wanted) == 1, f"id «{wanted}» должен быть в разметке ровно один"
    for wanted in WIRED_IDS:
        assert f"$('{wanted}')?.addEventListener" in HTML, (
            f"обработчик на #{wanted} навешивается без опциональной цепочки"
        )


def test_идентификаторы_не_дублируются():
    ids = markup().ids
    assert len(ids) == len(set(ids)), "getElementById нашёл бы не тот узел"


def test_страница_остаётся_самодостаточной():
    """Единственная внешняя ссылка — SDK Telegram; своих сборщиков у нас нет."""
    assert markup().external == ["https://telegram.org/js/telegram-web-app.js"]
