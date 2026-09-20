"""Вырезает тёмную тему из HTML Mini App. Остаётся только светлая.

Причина: команда посмотрела тёмный вариант и забраковала. Одна тема вместо
двух — это ещё и вдвое меньше мест, где вёрстка может разъехаться.

Скрипт убирает:
  * блок @media (prefers-color-scheme: dark) { ... } целиком;
  * блок :root[data-theme="dark"] { ... };
  * переключатель темы в разметке и обработчик к нему;
  * чтение Telegram.WebApp.colorScheme для выбора темы.
И проставляет data-theme="light" на <html>, чтобы ничто не могло переключить.

    python -m scripts.force_light_theme            # все файлы miniapp
    python -m scripts.force_light_theme путь.html  # конкретные
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MINIAPP = ROOT / "miniapp"


def cut_block(text: str, start_pattern: str) -> tuple[str, int]:
    """Удаляет CSS-блок целиком, считая вложенные фигурные скобки.

    Простой регуляркой тут не обойтись: @media содержит вложенные правила,
    и жадный поиск закрывающей скобки съел бы половину стилей.
    """
    removed = 0
    while True:
        match = re.search(start_pattern, text)
        if match is None:
            return text, removed
        open_at = text.find("{", match.start())
        if open_at == -1:
            return text, removed
        depth, index = 0, open_at
        while index < len(text):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        if depth != 0:
            return text, removed  # незакрытый блок — не трогаем от греха
        text = text[: match.start()] + text[index + 1 :]
        removed += 1


def strip_dark(html: str) -> tuple[str, list[str]]:
    notes: list[str] = []

    html, n = cut_block(html, r"@media[^{]*prefers-color-scheme:\s*dark[^{]*")
    if n:
        notes.append(f"@media dark: {n}")

    html, n = cut_block(html, r':root\[data-theme=["\']dark["\']\]\s*')
    if n:
        notes.append(f':root[data-theme=dark]: {n}')

    # Селекторы вида :root:not([data-theme="light"]) больше не нужны:
    # светлая тема теперь единственная.
    before = html
    html = re.sub(r':root:not\(\[data-theme=["\']light["\']\]\)', ":root", html)
    if html != before:
        notes.append("упрощены селекторы :not([data-theme=light])")

    # <html ...> получает жёстко светлую тему.
    def fix_html_tag(match: re.Match[str]) -> str:
        tag = match.group(0)
        tag = re.sub(r'\s*data-theme=["\'][^"\']*["\']', "", tag)
        return tag[:-1].rstrip() + ' data-theme="light">'

    html = re.sub(r"<html\b[^>]*>", fix_html_tag, html, count=1)

    # Переключатель темы в разметке.
    before = html
    html = re.sub(
        r'\s*<button[^>]*(?:id|class)=["\'][^"\']*(?:theme|toggle-theme)[^"\']*["\'][^>]*>.*?</button>',
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if html != before:
        notes.append("убран переключатель темы")

    # JS, который что-то делает с темой.
    lines = html.split("\n")
    kept, dropped = [], 0
    for line in lines:
        low = line.lower()
        touches_theme = (
            "data-theme" in low
            or "colorscheme" in low
            or "prefers-color-scheme" in low
        )
        # Строку с <html ... data-theme="light"> оставляем.
        if touches_theme and "<html" not in low:
            dropped += 1
            continue
        kept.append(line)
    if dropped:
        notes.append(f"убрано строк скрипта про тему: {dropped}")
    html = "\n".join(kept)

    return html, notes


def process(path: Path) -> bool:
    original = path.read_text(encoding="utf-8")
    updated, notes = strip_dark(original)
    if updated == original:
        print(f"  {path.name:<22} без изменений")
        return False
    path.write_text(updated, encoding="utf-8")
    print(f"  {path.name:<22} {'; '.join(notes)}")
    return True


def main() -> None:
    targets = (
        [Path(p) for p in sys.argv[1:]]
        if len(sys.argv) > 1
        else sorted(MINIAPP.rglob("*.html"))
    )
    print(f"Файлов: {len(targets)}")
    changed = sum(process(p) for p in targets)
    print(f"Изменено: {changed}")

    # Проверка: тёмной темы не должно остаться нигде.
    leftovers = []
    for path in targets:
        text = path.read_text(encoding="utf-8")
        if re.search(r"prefers-color-scheme:\s*dark", text) or 'data-theme="dark"' in text:
            leftovers.append(path.name)
    if leftovers:
        raise SystemExit("тёмная тема осталась в: " + ", ".join(leftovers))
    print("Тёмной темы не осталось нигде.")


if __name__ == "__main__":
    main()
