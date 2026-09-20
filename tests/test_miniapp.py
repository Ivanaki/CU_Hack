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
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "miniapp" / "dist" / "index.html"

SHORTCUT_URL = "https://www.icloud.com/shortcuts/f98896d3b29f48c6b95fae87187b46cf"
# Имя команды задано автором шортката и участвует в адресе запуска.
SHORTCUT_NAME = "бот тг"

HTML = INDEX.read_text(encoding="utf-8")
FLAT = re.sub(r"\s+", " ", HTML)

VOID_TAGS = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)

# Тексты согласованы с заказчиком и проверяются дословно: «примерно те же
# слова» здесь не годятся — по ним человек ищет пункты в чужом интерфейсе.
STEP_TITLES = (
    "Установите команду",
    "Активируйте команду",
    "Назначьте двойное касание",
    "Проверьте работу",
)

STEP_LINES = (
    "Нажмите кнопку «Установить команду».",
    "На открывшейся странице нажмите «Получить быструю команду».",
    "Нажмите «Добавить быструю команду».",
    "Не переименовывайте команду.",
    "Вернитесь в приложение.",
    "Откройте «Настройки» на iPhone.",
    "Выберите «Универсальный доступ».",
    "Откройте «Касание».",
    "Нажмите «Коснуться сзади».",
    "Выберите «Двойное касание».",
    "Пролистайте до раздела «Команды».",
    "Выберите «бот тг».",
)

TROUBLE_LINES = (
    "Проверьте, что команда называется «бот тг».",
    "Снова нажмите «Активировать команду».",
    "Один раз запустите команду вручную.",
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

# Обработчик ответа на вопрос — к нему обращаются два теста.
ANSWER_FN = r"async function answer\(q, wrap, btn, ownText\) \{(.+?)\n\}"

# Идентификаторы, за которые скрипт держится руками.
WIRED_IDS = (
    "howtoOpen", "howtoDone", "howtoClose", "howtoInstall",
    "howtoActivate", "howtoRetry",
)


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


def without_comments(code: str) -> str:
    """Код без комментариев: проверяем, что делает скрипт, а не что в нём написано."""
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.DOTALL)
    return re.sub(r"(?m)//.*$", " ", code)


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


def test_все_четыре_этапа_на_месте_и_пронумерованы():
    for title in STEP_TITLES:
        assert title in TEXT, f"этап «{title}» пропал"
    for number in ("1", "2", "3", "4"):
        assert f'<span class="step-n">{number}</span>' in HTML, f"этап {number} без номера"


def test_шаги_этапов_приведены_дословно():
    missing = [line for line in STEP_LINES if line not in TEXT]
    assert not missing, f"шаги разошлись с согласованным текстом: {missing}"


def test_проверка_работы_описана_дословно():
    """Единственный абзац без списка — в разметке он разбит переносом строк."""
    assert (
        "Откройте нужный экран и дважды постучите по задней панели iPhone "
        "около логотипа Apple. При первом запуске разрешите запрошенный доступ." in TEXT
    )


def test_путь_в_настройках_показан_одной_строкой():
    path = (
        "Настройки → Универсальный доступ → Касание → Коснуться сзади → "
        "Двойное касание → бот тг"
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


def test_активация_складывает_адрес_команды_по_спецификации():
    """Адрес запуска обязан совпасть с тем, что понимает сама команда.

    Имя «бот тг» кодируется целиком: пробел и кириллица в схеме
    shortcuts:// иначе обрывают адрес на первом небезопасном символе.
    Обратных слешей в схеме быть не должно — в ТЗ это оговорено отдельно.
    """
    body = re.search(r"function activationLink\(token\) \{(.+?)\n\}", HTML, re.DOTALL)
    assert body, "сборка адреса активации пропала"
    code = body.group(1)

    assert "'shortcuts://run-shortcut'" in code, "схема запуска команды задана ТЗ"
    assert "\\" not in code, "обратных слешей в адресе быть не должно"
    assert "encodeURIComponent(token)" in code, "ключ обязан быть закодирован"
    assert "encodeURIComponent(SHORTCUT_NAME)" in code, "имя команды обязано быть закодировано"
    assert "'&input=text'" in code, "команда принимает текст на вход"

    name = re.search(r"const SHORTCUT_NAME = '([^']+)';", HTML)
    assert name and name.group(1) == SHORTCUT_NAME, "имя команды разошлось с шорткатом"
    # То же самое, что сделает encodeURIComponent в браузере.
    assert quote(SHORTCUT_NAME, safe="") == "%D0%B1%D0%BE%D1%82%20%D1%82%D0%B3"


def test_ключ_берётся_с_сервера_а_не_из_кода():
    """Статический ключ означал бы, что чужие траты падают в одну книгу."""
    assert "test_token" not in HTML, "тестового ключа в бою быть не может"
    assert "fm_" not in HTML, "ключ не зашивается в страницу ни в каком виде"
    body = re.search(r"async function prepareActivation\(\) \{(.+?)\n\}", HTML, re.DOTALL)
    assert body, "получение ключа пропало"
    code = body.group(1)
    assert "api('/me/upload-link')" in code, "ключ выдаёт сервер по подписи Telegram"
    # Клиент не выбирает, за кого он: tg_id в запросе за ключом не участвует.
    assert "tg_id" not in code, "человека определяет сервер, а не параметр запроса"


def test_ключ_не_показывается_и_не_пишется_в_журнал():
    """Ключ — секрет: в интерфейсе он не нужен, в консоли опасен."""
    for name in ("prepareActivation", "runActivation"):
        body = re.search(rf"function {name}\(.*?\) \{{(.+?)\n\}}", HTML, re.DOTALL)
        assert body, f"обработчик {name} пропал"
        code = body.group(1)
        assert "console.log" not in code, "ключ не уходит в журнал"
        assert "token" not in code or name == "prepareActivation", (
            "в обработчике нажатия ключа быть не должно: он уже лежит в href"
        )
    prepare = re.search(
        r"async function prepareActivation\(\) \{(.+?)\n\}", HTML, re.DOTALL
    ).group(1)
    assert "textContent = " not in prepare.replace("box.textContent = text", ""), (
        "ключ не попадает в текст на экране"
    )
    assert "howtoState(data.token" not in HTML, "ключ не показывается человеку"


def test_без_ключа_кнопка_не_молчит():
    """Нажатие без ключа обязано объяснить себя, а не провалиться в тишину."""
    prepare = re.search(
        r"async function prepareActivation\(\) \{(.+?)\n\}", HTML, re.DOTALL
    ).group(1)
    assert "!data.token" in prepare, "пустой ответ сервера — отдельный случай"
    assert "removeAttribute('href')" in prepare, "без ключа ссылке некуда вести"

    run = re.search(r"function runActivation\(event\) \{(.+?)\n\}", HTML, re.DOTALL).group(1)
    assert "preventDefault" in run, "клик по неготовой кнопке не должен никуда уводить"
    assert "Повторить активацию" in TEXT, "человеку нужен способ попробовать ещё раз"
    assert "catch (e)" in prepare, "сетевая ошибка тоже показывается"


def test_активация_не_идёт_через_openLink():
    """Проверено на живом телефоне: встроенное окно Telegram схему
    shortcuts:// не понимает и молчит — кнопка выглядит сломанной.

    Работает другой путь: обычная внешняя ссылка открывается в Safari, а
    Safari передаёт управление «Командам». Значит, никакого openLink и
    никакого preventDefault: ссылку отрабатывает сам клиент.
    """
    run = re.search(r"function runActivation\(event\) \{(.+?)\n\}", HTML, re.DOTALL)
    assert run, "обработчик активации пропал"
    # Комментарии выкидываем: объяснение, почему openLink звать нельзя, само
    # содержит это слово — и тест ловил бы собственное объяснение.
    code = without_comments(run.group(1))
    assert "openLink" not in code, "openLink не открывает shortcuts:// — кнопка замолчит"
    # preventDefault остаётся только для случая «ключа ещё нет».
    guarded = code.split("return;", 1)
    assert len(guarded) == 2 and "preventDefault" not in guarded[1], (
        "перехват клика убивает переход в Safari"
    )
    assert "window.location.href = href" in code, "нужен запасной переход"
    assert "document.hidden" in code, "запасной переход не должен открывать второй раз"

    activate = [a for a in markup().links if a.get("id") == "howtoActivate"]
    assert len(activate) == 1, "кнопка активации — настоящая ссылка, а не кнопка"
    assert activate[0].get("target") == "_blank", "ссылка обязана уходить наружу, в Safari"
    assert activate[0].get("rel") == "noopener noreferrer"


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


def test_ответ_своими_словами_есть_в_каждом_вопросе():
    """Готовые варианты закрывают частые случаи, но не все.

    «Перевод Дмитрий К. на 25 000 ₽» бывает арендой, возвратом за ремонт,
    долей за поездку. Без поля ввода человек откладывает вопрос, и деньги
    остаются необъяснёнными — то есть метрика продукта не растёт.
    """
    queue = re.search(r"function renderQueue\(focusId\) \{(.+?)\n\}", HTML, re.DOTALL)
    assert queue, "отрисовка очереди пропала"
    code = without_comments(queue.group(1))
    assert 'class="ans own"' in code, "кнопка «своими словами» рисуется"
    # Но только если сервер этот вариант прислал: кнопка, которой на
    # сервере нет, приводит человека к ошибке после набранного ответа.
    assert "o.code === 'free'" in code, "вариант берётся из ответа сервера"
    assert "(free" in code, "без варианта с сервера кнопка не рисуется"

    open_own = re.search(r"function openOwnAnswer\(q, wrap, btn\) \{(.+?)\n\}", HTML, re.DOTALL)
    assert open_own, "поле ввода не открывается"
    body = without_comments(open_own.group(1))
    assert "qfree-in" in body and "maxlength=\"200\"" in body
    assert "input.focus()" in body, "поле должно сразу принимать ввод"
    # Пустой ответ не отправляется: молча закрыть вопрос — обмануть человека.
    assert "if (!text)" in body
    assert "'Enter'" in body, "ввод завершается с клавиатуры, а не только кнопкой"

    answer = re.search(ANSWER_FN, HTML, re.DOTALL)
    assert answer, "обработчик ответа пропал"
    sent = without_comments(answer.group(1))
    assert "{code: 'free', text: ownText}" in sent, "свободный ответ уходит своим кодом"


def test_отвеченная_карточка_уезжает_одна():
    """Ответ обязан выглядеть как «карточка уехала», а не как мигание списка.

    Раньше после ответа очередь перерисовывалась целиком, и все оставшиеся
    карточки заново проигрывали своё появление — дважды, потому что за
    перерисовкой шло тихое обновление данных.
    """
    answer = re.search(ANSWER_FN, HTML, re.DOTALL)
    code = without_comments(answer.group(1))
    assert "wrap.classList.add('gone')" in code, "уход карточки анимируется"
    assert "wrap.remove()" in code, "ушедшая карточка убирается точечно"
    assert "renderQueue()" not in code, "полная перерисовка переигрывает весь список"

    # Тихое обновление тоже не должно перерисовывать очередь без причины.
    refresh = re.search(r"async function refresh\(silent, quiet\) \{(.+?)\n\}", HTML, re.DOTALL)
    assert refresh and "if (!queueMatches()) renderQueue()" in without_comments(refresh.group(1))

    # Уход карточки — это схлопывание строки плюс отъезд самой карточки.
    assert ".qwrap.gone{grid-template-rows:0fr}" in FLAT
    assert ".qwrap.gone .quiz{" in FLAT

    # Счётчики в оставшихся карточках правятся на месте, а пустая очередь
    # показывает итог — иначе на экране остаётся «3 в очереди» при нуле.
    drop = re.search(r"function dropQueueCard\(\) \{(.+?)\n\}", HTML, re.DOTALL)
    assert drop, "счётчики очереди никто не обновляет"
    assert "alldone pop" in drop.group(1), "пустая очередь показывает итог"


def test_страница_остаётся_самодостаточной():
    """Единственная внешняя ссылка — SDK Telegram; своих сборщиков у нас нет."""
    assert markup().external == ["https://telegram.org/js/telegram-web-app.js"]
