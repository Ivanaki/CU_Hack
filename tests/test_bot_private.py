"""Бот в общем чате: ни цифр, ни личного ключа.

Ключ загрузки — это доступ на запись в книгу человека, и опубликовать его
в групповом чате означает отдать книгу всем участникам. Privacy-mode
телеграма здесь не спасает: команда, адресованная боту явно
(`/link@honest_month_bot`), доходит до него именно в таком виде. Хуже
того, инлайн-кнопка под таким сообщением активна для любого участника, и
«Выпустить новую» заводила ключ уже нажавшему — прямо в общем чате.

Проверяется настоящим прогоном обновления через диспетчер: фильтры
хендлеров — это как раз то, что ломается молча.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetFile, SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    File,
    Message,
    MessageEntity,
    PhotoSize,
    Update,
)
from aiogram.types import User as TgUser

from tests.test_uploads import png

BASE_URL = "https://example.test"
MINIAPP_URL = "https://example.test/app"
BOT_TOKEN = "424242:TESTTESTTESTTESTTESTTESTTESTTESTTES"
BOT_NAME = "honest_month_bot"

GROUP = Chat(id=-1001234567890, type="supergroup", title="Коллеги по работе")
PRIVATE = Chat(id=991_501, type="private")

OWNER = TgUser(id=991_501, is_bot=False, first_name="Хозяин")
OUTSIDER = TgUser(id=991_502, is_bot=False, first_name="Посторонний")


class Recorder(BaseSession):
    """Сессия, которая никуда не ходит, а записывает вызовы к Telegram."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list = []

    async def close(self) -> None:  # pragma: no cover — закрывать нечего
        return None

    async def stream_content(self, *args, **kwargs):
        # Ровно то, что телефон прислал бы картинкой.
        yield png()

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            # Привязываем к боту руками: обычно это делает разбор ответа,
            # а мы его обошли — без привязки note.edit_text не вызвать.
            return Message(
                message_id=len(self.calls) + 100,
                date=datetime.now(),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            ).as_(bot)
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="u", file_path="photo.png")
        if isinstance(method, EditMessageText):
            return True
        return True

    def sent(self) -> list[SendMessage]:
        return [call for call in self.calls if isinstance(call, SendMessage)]

    def alerts(self) -> list[AnswerCallbackQuery]:
        return [call for call in self.calls if isinstance(call, AnswerCallbackQuery)]


@pytest.fixture
def bot(_isolated_db, tmp_path):
    saved = {
        name: os.environ.get(name)
        for name in ("PUBLIC_BASE_URL", "UPLOAD_DIR", "MINIAPP_URL")
    }
    os.environ["PUBLIC_BASE_URL"] = BASE_URL
    os.environ["UPLOAD_DIR"] = str(tmp_path)
    # Адрес приложения задаём явно: в .env разработчика он есть, и тест не
    # должен зависеть от того, что там сейчас написано.
    os.environ["MINIAPP_URL"] = MINIAPP_URL

    from app.config import get_settings

    get_settings.cache_clear()
    from app.db import init_db

    init_db()

    session = Recorder()
    instance = Bot(BOT_TOKEN, session=session)
    # Имя бота нужно фильтру команд: `/link@honest_month_bot` он сверяет с
    # ним. Подставляем заранее, чтобы тест не ходил в сеть за getMe.
    instance._me = TgUser(id=42, is_bot=True, first_name="Честный месяц", username=BOT_NAME)
    yield instance

    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    get_settings.cache_clear()


def message(chat: Chat, sender: TgUser, text: str) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(),
        chat=chat,
        from_user=sender,
        text=text,
        entities=[MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))],
    )


async def feed(bot: Bot, **payload) -> None:
    from app.bot.main import dp

    await dp.feed_update(bot, Update(update_id=1, **payload))


def token_of(tg_id: int) -> str | None:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import User

    with session_scope() as db:
        user = db.scalar(select(User).where(User.tg_id == tg_id))
        return user.upload_token if user else None


async def test_ключ_не_уезжает_в_групповой_чат(bot):
    """`/link@бот` в супергруппе — публикация доступа всем участникам."""
    await feed(bot, message=message(GROUP, OWNER, f"/link@{BOT_NAME}"))

    sent = bot.session.sent()
    assert len(sent) == 1
    answer = sent[0]

    assert answer.chat_id == GROUP.id
    assert "fm_" not in (answer.text or ""), "личный ключ в общем чате"
    assert "личный ключ" in answer.text
    # Отказ обязан показывать дорогу, иначе читается как поломка.
    assert f"t.me/{BOT_NAME}?start=link" in answer.text


async def test_кнопка_выпуска_в_группе_не_заводит_ключ_постороннему(bot):
    """Кнопку под чужим сообщением нажимает кто угодно — и получал свой ключ."""
    query = CallbackQuery(
        id="1",
        from_user=OUTSIDER,
        chat_instance="group-instance",
        data="upload:rotate",
        message=Message(
            message_id=2, date=datetime.now(), chat=GROUP, text="Ваша ссылка"
        ),
    )

    await feed(bot, callback_query=query)

    assert not bot.session.sent(), "в общий чат не должно уйти ни одного сообщения"
    assert bot.session.alerts(), "человеку надо объяснить, почему ничего не произошло"
    assert bot.session.alerts()[0].show_alert is True
    assert token_of(OUTSIDER.id) is None, "посторонний не должен был получить ключ"


async def test_итоги_месяца_в_группу_не_уходят(bot):
    """Деньги — такой же личный ответ, как и ключ."""
    await feed(bot, message=message(GROUP, OWNER, f"/month@{BOT_NAME}"))

    sent = bot.session.sent()
    assert len(sent) == 1
    assert "₽" not in (sent[0].text or "")
    assert "Здесь я молчу" in sent[0].text


async def test_в_личном_чате_ключ_приходит_и_защищён(bot):
    """В личке всё работает — и сообщение с ключом не пересылается дальше."""
    await feed(bot, message=message(PRIVATE, OWNER, "/link"))

    sent = bot.session.sent()
    assert len(sent) == 1
    answer = sent[0]
    token = token_of(OWNER.id)

    assert token and token in answer.text
    assert answer.chat_id == PRIVATE.id
    assert answer.protect_content is True, "пересылка сообщения с ключом — способ его отдать"
    # Ключ под спойлером: иначе он виден в превью чата и в пуше на
    # заблокированном экране, то есть без участия человека.
    assert f"<tg-spoiler><code>{token}</code></tg-spoiler>" in answer.text
    # А предупреждение — над ключом: скриншот верхней части сообщения
    # обязан приехать вместе с ним.
    assert answer.text.index("Кто его увидит") < answer.text.index(token)
    # Путь в репозитории с телефона не открыть, и в тексте его больше нет.
    assert "SHORTCUT.md" not in answer.text


async def test_deep_link_из_группы_приводит_за_ключом_в_личку(bot):
    """Ссылка из группового отказа обязана сразу давать то, за чем пришли."""
    await feed(bot, message=message(PRIVATE, OUTSIDER, "/start link"))

    sent = bot.session.sent()
    assert len(sent) == 1
    assert token_of(OUTSIDER.id), "ключ выпущен"
    assert token_of(OUTSIDER.id) in sent[0].text
    assert sent[0].chat_id == PRIVATE.id


async def test_картинка_из_бота_сохраняется_и_остаётся_личной(bot, monkeypatch):
    """У бота карточка «Что я присылал» была без файла — смотреть не на что."""
    from datetime import datetime as dt

    from app.ingest import vision as vision_mod
    from app.ingest.base import ParsedRow

    monkeypatch.setattr(
        vision_mod,
        "parse_images",
        lambda images, **kwargs: vision_mod.VisionResult(
            "statement",
            [
                ParsedRow(
                    booked_at=dt(2026, 9, 14, 12, 0),
                    amount=-33000,
                    description="Кофейня из бота",
                    bank="Т-Банк",
                )
            ],
            "Т-Банк",
        ),
    )

    photo = Message(
        message_id=3,
        date=datetime.now(),
        chat=PRIVATE,
        from_user=OWNER,
        photo=[PhotoSize(file_id="f1", file_unique_id="u1", width=1170, height=2532,
                         file_size=len(png()))],
    )
    await feed(bot, message=photo)

    from sqlalchemy import select

    from app.db import session_scope
    from app.models import ImportBatch, User

    with session_scope() as db:
        user = db.scalar(select(User).where(User.tg_id == OWNER.id))
        batch = db.scalars(
            select(ImportBatch)
            .where(ImportBatch.user_id == user.id)
            .order_by(ImportBatch.id.desc())
        ).first()

        assert batch.channel == "bot"
        assert batch.stored_as, "по файлу должно быть видно, что именно человек прислал"
        assert batch.content_type == "image/png"

    from app.api.uploads import PRIVATE_DIR
    from app.config import get_settings

    # И лежит он там же, где файлы шортката: в закрытом каталоге.
    assert (get_settings().upload_path / PRIVATE_DIR / batch.stored_as).is_file()


async def test_старт_отвечает_короткой_строкой_без_списка_команд(bot):
    """Первый экран — это кнопка, а не стена текста.

    Раньше /start отдавал шесть абзацев со списком команд и методикой:
    человек открывал бота и не понимал, куда тыкать. Всё это осталось в
    /help, а /start обязан читаться за секунду.
    """
    await feed(bot, message=message(PRIVATE, OWNER, "/start"))

    sent = bot.session.sent()
    assert len(sent) == 1
    text = sent[0].text

    # Прежняя простыня была больше 600 знаков; запас оставлен на правки
    # формулировки, но не на возврат абзацев.
    assert len(text) < 200, f"/start снова разросся: {len(text)} знаков"
    assert text.count("\n") <= 2, "две строки максимум"
    for command in ("/month", "/week", "/questions", "/link", "/today", "/streak"):
        assert command not in text, f"список команд вернулся в /start: {command}"


async def test_старт_даёт_кнопку_в_приложение_первой(bot):
    """Открыть Mini App должно быть первым и самым заметным действием."""
    await feed(bot, message=message(PRIVATE, OWNER, "/start"))

    markup = bot.session.sent()[0].reply_markup
    assert markup is not None, "к сообщению не приложено ни одной кнопки"
    first = markup.keyboard[0][0]

    assert first.web_app is not None, "первая кнопка обязана открывать приложение"
    assert first.web_app.url == MINIAPP_URL
    assert "риложени" in first.text


async def test_старт_без_адреса_приложения_объясняет_словами(bot):
    """Пустой MINIAPP_URL — это настройка, а не повод падать.

    Мёртвая кнопка хуже её отсутствия: человек тапает и не понимает,
    почему ничего не открылось. Поэтому кнопки нет, а текст говорит, что
    именно нужно сделать.
    """
    from app.config import get_settings

    os.environ["MINIAPP_URL"] = ""
    get_settings.cache_clear()
    try:
        await feed(bot, message=message(PRIVATE, OWNER, "/start"))
    finally:
        os.environ["MINIAPP_URL"] = MINIAPP_URL
        get_settings.cache_clear()

    sent = bot.session.sent()
    assert len(sent) == 1, "бот обязан ответить, а не промолчать"
    text = sent[0].text

    assert "MINIAPP_URL" in text, "человеку нужно сказать, чего не хватает"
    markup = sent[0].reply_markup
    buttons = [button for row in (markup.keyboard if markup else []) for button in row]
    assert all(button.web_app is None for button in buttons), "кнопка вела бы в никуда"
