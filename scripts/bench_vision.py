"""Замер: какая модель действительно читает скриншот банка.

Модель для зрения выбирается по измеренной точности на эталонном
скриншоте, а не по названию. Скрипт гоняет настоящий код разбора
(app/ingest/vision), поэтому меряется весь путь целиком, включая промпт и
разбор ответа.
"""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.ingest.vision import parse_screenshots  # noqa: E402
from app.llm.client import LLM  # noqa: E402

IMAGE = ROOT / "data" / "demo" / "screenshot_tbank.png"
TODAY = date(2026, 9, 22)

# Эталон: то, что человек видит на картинке.
EXPECTED: set[tuple[str, int]] = {
    ("2026-09-22", 50_000),
    ("2026-09-22", -154_000),
    ("2026-09-22", -59_000),
    ("2026-09-21", -548_000),
    ("2026-09-21", -32_000),
    ("2026-09-19", 150_000),
    ("2026-09-19", -124_000),
    ("2026-09-19", -231_000),
}

CANDIDATES = [
    "google/gemini-3.6-flash",
    "google/gemini-3.5-flash-lite",
    "google/gemini-2.5-flash",
    "openai/gpt-5-mini",
    "qwen/qwen3-vl-30b-a3b-instruct",
    "qwen/qwen3.6-35b-a3b",
    "sber/gigachat-2-pro",
    "anthropic/claude-haiku-4.5",
]


def score(model: str, blob: bytes) -> tuple[float, int, int, float, str]:
    settings = get_settings().model_copy(update={"llm_model_vision": model})
    llm = LLM(settings)
    started = time.time()
    try:
        rows = parse_screenshots([blob], today=TODAY, llm=llm)
    except Exception as exc:  # noqa: BLE001 — замер не должен падать целиком
        return 0.0, 0, 0, time.time() - started, f"{type(exc).__name__}"
    got = {(r.booked_at.date().isoformat(), r.amount) for r in rows}
    hits = len(got & EXPECTED)
    extra = len(got - EXPECTED)
    elapsed = time.time() - started
    accuracy = hits / len(EXPECTED)
    note = "" if not extra else f"{extra} лишних"
    return accuracy, hits, extra, elapsed, note


def main() -> None:
    if not IMAGE.exists():
        print("нет скриншота, сначала: python scripts/make_screenshot.py")
        return
    blob = IMAGE.read_bytes()
    print(f"Эталон: {len(EXPECTED)} операций, файл {IMAGE.stat().st_size // 1024} КБ\n")
    print(f"{'модель':<34}{'точно':>7}{'лишних':>8}{'сек':>7}  примечание")
    print("-" * 74)
    results = []
    for model in CANDIDATES:
        accuracy, hits, extra, elapsed, note = score(model, blob)
        results.append((accuracy, -extra, -elapsed, model))
        print(
            f"{model:<34}{hits:>3}/{len(EXPECTED):<3}{extra:>8}{elapsed:>7.1f}  {note}"
        )
    results.sort(reverse=True)
    print("-" * 74)
    print(f"Лучшая: {results[0][3]}")


if __name__ == "__main__":
    main()
