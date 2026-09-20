"""Рисует реалистичный скриншот списка операций банковского приложения.

Нужен, чтобы проверять распознавание без настоящих персональных данных и
чтобы на демонстрации было что переслать боту.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "demo" / "screenshot_tbank.png"

W, H = 900, 1500
BG = (250, 250, 252)
CARD = (255, 255, 255)
INK = (24, 24, 27)
MUTED = (130, 132, 140)
GREEN = (34, 160, 90)
ACCENT = (255, 221, 45)

FONT_CANDIDATES = (
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
FONT_BOLD_CANDIDATES = (
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path in FONT_BOLD_CANDIDATES if bold else FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


# (группа, название, подпись, сумма, время)
GROUPS: list[tuple[str, list[tuple[str, str, float, str]]]] = [
    ("Сегодня, 22 сентября", [
        ("Катя Н.", "Перевод СБП · за обед", 500, "13:37"),
        ("Пятёрочка", "Супермаркеты", -1_540, "11:04"),
        ("Яндекс Go", "Такси", -590, "09:12"),
    ]),
    ("Вчера, 21 сентября", [
        ("Лента", "Супермаркеты", -5_480, "19:21"),
        ("Surf Coffee", "Кафе и рестораны", -320, "08:40"),
    ]),
    ("19 сентября", [
        ("Иван П.", "Перевод СБП · возврат части долга", 1_500, "12:40"),
        ("Яндекс Go", "Такси", -1_240, "18:05"),
        ("Wildberries", "Маркетплейсы", -2_310, "14:52"),
    ]),
]


def draw() -> Path:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Шапка
    d.rectangle([0, 0, W, 190], fill=CARD)
    d.text((40, 46), "Т-Банк", font=font(34, True), fill=INK)
    d.rounded_rectangle([W - 190, 40, W - 40, 86], 12, fill=ACCENT)
    d.text((W - 172, 52), "Black", font=font(22, True), fill=INK)
    d.text((40, 104), "Дебетовая карта · 4127", font=font(24), fill=MUTED)
    d.text((40, 140), "542 416 ₽", font=font(32, True), fill=INK)

    y = 220
    for title, items in GROUPS:
        d.text((40, y), title, font=font(24, True), fill=MUTED)
        y += 46
        height = len(items) * 104 + 16
        d.rounded_rectangle([28, y, W - 28, y + height], 24, fill=CARD)
        row = y + 16
        for name, note, amount, when in items:
            d.ellipse([52, row + 20, 116, row + 84], fill=(238, 239, 243))
            d.text((70, row + 36), name[:1].upper(), font=font(30, True), fill=MUTED)
            d.text((140, row + 26), name, font=font(27, True), fill=INK)
            d.text((140, row + 62), note, font=font(21), fill=MUTED)
            text = f"{'+' if amount > 0 else '−'}{abs(amount):,.0f} ₽".replace(",", " ")
            colour = GREEN if amount > 0 else INK
            width = d.textlength(text, font=font(27, True))
            d.text((W - 60 - width, row + 26), text, font=font(27, True), fill=colour)
            tw = d.textlength(when, font=font(20))
            d.text((W - 60 - tw, row + 62), when, font=font(20), fill=MUTED)
            row += 104
        y += height + 34

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, "PNG")
    return OUT


if __name__ == "__main__":
    path = draw()
    print(f"Скриншот: {path.relative_to(ROOT)} ({path.stat().st_size // 1024} КБ)")
    sys.exit(0)
