"""Картинка -> список операций.

Два вида картинок, один вызов модели. Пользователь не должен выбирать в
меню, что именно он присылает: он просто кидает картинку, а система сама
решает, скриншот это из приложения банка или фотография чека.

    скриншот банка — много операций, каждая со своей датой и суммой
    чек из магазина — одна трата, зато с составом покупки

Скриншот нужен между выписками, чтобы аналитика была свежей, а спорные
операции всплывали сегодня, а не через месяц. Выписка остаётся основным
источником: она полная и точная.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.ingest.base import ParsedRow
from app.llm.client import LLM, get_llm
from app.models import OperationSource

log = logging.getLogger(__name__)

SYSTEM = (
    "Ты извлекаешь финансовые операции из картинок: скриншотов мобильных "
    "приложений российских банков и фотографий кассовых чеков. Отвечаешь "
    "только JSON, без пояснений. Ничего не выдумываешь: чего не видно на "
    "картинке, то null."
)

PROMPT = """Определи, что на картинке, и извлеки операции.

Верни JSON строго такой формы:
{{
  "kind": "statement" | "receipt" | "other",
  "bank": "название банка или магазина, либо null",
  "card_last4": "последние 4 цифры карты или null",
  "operations": [
    {{
      "date": "YYYY-MM-DD",
      "time": "HH:MM или null",
      "amount": -1250.50,
      "merchant": "как написано на картинке",
      "description": "пояснение под названием или состав чека, иначе null",
      "category_hint": "категория, если она показана, иначе null"
    }}
  ]
}}

Как определять вид:
- "statement" — список операций из приложения банка: несколько строк
  с суммами, датами, названиями продавцов.
- "receipt" — кассовый чек: один магазин, перечень товаров, ИТОГО.
  Верни ОДНУ операцию на сумму ИТОГО (не по каждому товару),
  а перечень товаров кратко положи в description.
- "other" — на картинке нет операций. Верни пустой список operations.

Правила:
- amount отрицательный для трат и списаний, положительный для поступлений.
- Сегодня {today}. «Сегодня», «Вчера», «12 сентября» переводи в абсолютную
  дату. Год бери текущий; если дата выходит в будущем — бери прошлый год.
- Строки «Ожидается», «В обработке», «Заблокировано» тоже включай.
- Не включай остаток на счёте, итоговые суммы за период, кэшбэк-баллы,
  рекламные блоки и строки скидок внутри чека."""


@dataclass(slots=True)
class VisionResult:
    kind: str  # statement | receipt | other
    rows: list[ParsedRow]
    source_name: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.rows)

    @property
    def human_kind(self) -> str:
        return {"statement": "скриншот из банка", "receipt": "чек"}.get(
            self.kind, "картинка"
        )


def parse_images(
    images: list[bytes],
    *,
    today: date | None = None,
    llm: LLM | None = None,
    bank_hint: str | None = None,
) -> VisionResult:
    """Разбирает картинки в операции. Пустой результат — не ошибка, а «не вышло»."""
    llm = llm or get_llm()
    today = today or date.today()
    if not llm.available or not images:
        return VisionResult("other", [])

    data = llm.vision_json(
        PROMPT.format(today=today.isoformat()),
        images,
        system=SYSTEM,
        mime=_image_mime(images[0]),
    )
    if not isinstance(data, dict):
        return VisionResult("other", [])

    kind = str(data.get("kind") or "statement")
    source = bank_hint or (data.get("bank") or ("Чек" if kind == "receipt" else "Банк"))
    last4 = _clean_last4(data.get("card_last4"))

    rows: list[ParsedRow] = []
    for item in data.get("operations") or []:
        row = _row(item, bank=source, last4=last4, today=today)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda r: r.booked_at)
    log.info("картинка (%s): распознано %d операций, источник %s", kind, len(rows), source)
    return VisionResult(kind, rows, source)


def parse_screenshots(
    images: list[bytes],
    *,
    today: date | None = None,
    llm: LLM | None = None,
    bank_hint: str | None = None,
) -> list[ParsedRow]:
    """Совместимость: только строки, без вида картинки."""
    return parse_images(images, today=today, llm=llm, bank_hint=bank_hint).rows


def _image_mime(blob: bytes) -> str:
    """Определяет тип картинки по сигнатуре.

    Раньше здесь стояло жёсткое "image/png". Скриншот из Telegram и правда
    приходит в PNG, но фотография чека с айфона — это JPEG или HEIC, и
    провайдеры такой data-URI с чужим ярлыком просто не принимают.
    """
    if blob[:8] == bytes.fromhex("89504e470d0a1a0a"):
        return "image/png"
    if blob[:3] == bytes.fromhex("ffd8ff"):
        return "image/jpeg"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    if blob[4:8] == b"ftyp":
        brand = blob[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return "image/heic"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    # Не опознали — пусть решает принимающая сторона по содержимому.
    return "application/octet-stream"


def _clean_last4(value: object) -> str | None:
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    return text[-4:] if len(text) >= 4 else None


def _row(item: dict, *, bank: str, last4: str | None, today: date) -> ParsedRow | None:
    try:
        amount = float(item.get("amount"))
    except (TypeError, ValueError):
        return None
    if amount == 0:
        return None

    when = _parse_date(item.get("date"), today)
    if when is None:
        return None
    hour, minute = 12, 0
    time_part = str(item.get("time") or "").strip()
    if ":" in time_part:
        try:
            hour, minute = (int(p) for p in time_part.split(":")[:2])
        except ValueError:
            hour, minute = 12, 0
    booked_at = datetime(when.year, when.month, when.day, min(hour, 23), min(minute, 59))

    merchant = str(item.get("merchant") or "").strip()
    description = str(item.get("description") or "").strip()
    return ParsedRow(
        booked_at=booked_at,
        amount=int(round(amount * 100)),
        description=" ".join(x for x in (merchant, description) if x)[:500],
        counterparty_raw=merchant or description,
        bank=bank,
        last4=last4,
        category_hint=(item.get("category_hint") or None),
        raw={"vision": True, **item},
    )


def _parse_date(value: object, today: date) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = date.fromisoformat(text[:10])
    except ValueError:
        return None
    # Защита от галлюцинации года: будущее невозможно.
    if parsed > today + timedelta(days=1):
        try:
            parsed = parsed.replace(year=parsed.year - 1)
        except ValueError:
            return None
    return parsed


SCREENSHOT_SOURCE = OperationSource.SCREENSHOT
