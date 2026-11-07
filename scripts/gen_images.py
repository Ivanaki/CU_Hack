"""Генерация картинок для Mini App через polza.ai.

Генерация асинхронная: POST /images/generations отдаёт requestId, готовый
файл забирается через GET /images/{requestId}.

Стоит примерно 8 ₽ за картинку, поэтому скрипт:
  * не перегенерирует то, что уже лежит на диске (--force для обхода);
  * считает и печатает потраченное;
  * иконки берёт одним листом-сеткой и режет локально — двенадцать иконок
    за одну генерацию вместо двенадцати, и стиль у них заведомо единый.

    python -m scripts.gen_images --list
    python -m scripts.gen_images icons empty
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402

OUT = ROOT / "miniapp" / "dist" / "art"
MODEL = "google/gemini-3.1-flash-image"
PRICE_HINT = 8.0  # рублей за картинку, по факту замера


@dataclass(frozen=True, slots=True)
class Job:
    name: str
    prompt: str
    grid: tuple[int, int] | None = None  # (столбцов, строк) — если это лист иконок
    slices: tuple[str, ...] = ()  # имена нарезанных кусков


STYLE = (
    "Flat vector illustration, thick rounded shapes, no gradients on shapes, "
    "friendly and playful, limited palette: fresh green #4CC86A, deep green #1F7A3D, "
    "warm yellow #FFD24A, coral #FF7A59, soft cream #FFF6E6, charcoal #1B2430. "
    "Crisp edges, no text, no letters, no numbers, no watermark. "
)

JOBS: tuple[Job, ...] = (
    Job(
        name="icons",
        grid=(4, 3),
        # Порядок сверен с тем, что модель нарисовала на самом деле:
        # просить сетку можно, гарантировать порядок — нет.
        slices=(
            "groceries", "cafe", "taxi", "transport",
            "housing", "home", "connection", "clothes",
            "entertainment", "electronics", "subscriptions", "other",
        ),
        prompt=(
            STYLE
            + "A neat 4 by 3 grid of 12 separate app icons on a pure white background, "
            "each icon centered inside its own equal invisible square cell with generous "
            "even margins, identical visual weight and identical style across all icons, "
            "no frames, no borders, no labels. Reading order left to right, top to bottom: "
            "1 grocery basket with vegetables, 2 coffee cup and croissant, 3 taxi car, "
            "4 metro train, 5 house with a roof, 6 mobile phone with signal waves, "
            "7 medical cross and pill, 8 t-shirt on a hanger, 9 laptop computer, "
            "10 cinema ticket and popcorn, 11 circular arrows renewal symbol, "
            "12 question mark inside a rounded square."
        ),
    ),
    Job(
        name="icons2",
        grid=(4, 3),
        # Второй лист: категории, которых не было в первом. Имена
        # сверяются с реальным результатом после генерации.
        slices=(
            "delivery", "health", "beauty", "sport",
            "gifts", "kids", "travel", "car",
            "education", "pets", "fees", "taxes",
        ),
        prompt=(
            STYLE
            + "A neat 4 by 3 grid of 12 separate app icons on a pure white background, "
            "each icon centered inside its own equal invisible square cell with generous "
            "even margins, identical visual weight and identical style across all icons, "
            "no frames, no borders, no labels. Reading order left to right, top to bottom: "
            "1 food delivery scooter, 2 medical cross with a pill, 3 hairdryer and comb, "
            "4 dumbbell, 5 gift box with a ribbon, 6 teddy bear toy, 7 airplane, "
            "8 fuel pump for a car, 9 stack of books, 10 dog paw print, "
            "11 bank building with columns, 12 paper document with a stamp."
        ),
    ),
    Job(
        name="empty",
        prompt=(
            STYLE
            + "A cheerful round golden coin character with big friendly eyes and a small "
            "smile, giving a thumbs up, standing next to a tidy stack of neatly sorted "
            "paper receipts, soft cream background, plenty of empty space around, "
            "square composition, centered."
        ),
    ),
    Job(
        name="celebrate",
        prompt=(
            STYLE
            + "A cheerful round golden coin character jumping with joy, arms up, "
            "confetti and small sparks flying around, warm yellow background, "
            "square composition, centered, sense of celebration."
        ),
    ),
    Job(
        name="puzzled",
        prompt=(
            STYLE
            + "A round golden coin character with a puzzled expression, one eyebrow raised, "
            "scratching its head, a small floating question mark beside it, "
            "soft cream background, square composition, centered."
        ),
    ),
    Job(
        name="pattern",
        prompt=(
            STYLE
            + "A very subtle seamless background pattern of tiny scattered coins, banknotes "
            "and receipts, extremely low contrast, almost monochrome pale green on cream, "
            "delicate and unobtrusive, suitable as a faint app background texture, "
            "seamless tileable, no focal point."
        ),
    ),
)

BY_NAME = {job.name: job for job in JOBS}


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def generate(job: Job, key: str, base: str, *, timeout: int = 300) -> bytes | None:
    body = json.dumps({"model": MODEL, "prompt": job.prompt, "n": 1}).encode()
    request = urllib.request.Request(
        f"{base}/images/generations", data=body, headers=_headers(key)
    )
    try:
        started = json.load(urllib.request.urlopen(request, timeout=120))
    except urllib.error.HTTPError as exc:
        print(f"  не отправилось: HTTP {exc.code} {exc.read()[:160].decode(errors='replace')}")
        return None

    request_id = started.get("requestId") or started.get("id")
    if not request_id:
        print(f"  ответ без requestId: {str(started)[:160]}")
        return None

    deadline = time.time() + timeout
    while time.time() < deadline:
        poll = urllib.request.Request(
            f"{base}/images/{request_id}", headers={"Authorization": f"Bearer {key}"}
        )
        try:
            state = json.load(urllib.request.urlopen(poll, timeout=60))
        except urllib.error.HTTPError:
            time.sleep(3)
            continue
        status = str(state.get("status", "")).upper()
        if status in {"COMPLETED", "SUCCESS", "DONE"}:
            url = state.get("url") or (state.get("images") or [None])[0]
            if not url:
                print("  готово, но без ссылки на файл")
                return None
            return urllib.request.urlopen(url, timeout=120).read()
        if status in {"FAILED", "ERROR", "CANCELED"}:
            print(f"  генерация не удалась: {state}")
            return None
        time.sleep(3)
    print("  не дождался результата")
    return None


def slice_sheet(blob: bytes, job: Job) -> int:
    """Режет лист-сетку на отдельные иконки и подрезает поля вокруг каждой."""
    import io

    from PIL import Image  # noqa: PLC0415

    sheet = Image.open(io.BytesIO(blob)).convert("RGBA")
    cols, rows = job.grid
    cell_w, cell_h = sheet.width // cols, sheet.height // rows
    saved = 0
    for index, name in enumerate(job.slices):
        if index >= cols * rows:
            break
        cx, cy = index % cols, index // cols
        cell = sheet.crop((cx * cell_w, cy * cell_h, (cx + 1) * cell_w, (cy + 1) * cell_h))
        cell = _trim_white(cell)
        cell.thumbnail((256, 256), Image.LANCZOS)
        cell.save(OUT / f"icon-{name}.png", optimize=True)
        saved += 1
    return saved


def _trim_white(image):  # noqa: ANN001, ANN201 — PIL.Image
    """Обрезает белые поля вокруг иконки, чтобы все пришли к одному масштабу."""
    from PIL import Image, ImageChops  # noqa: PLC0415

    background = Image.new("RGB", image.size, (255, 255, 255))
    background.paste(image.convert("RGB"), mask=image.split()[-1])
    diff = ImageChops.difference(background, Image.new("RGB", image.size, (255, 255, 255)))
    box = diff.getbbox()
    if box is None:
        return image
    pad = 8
    left = max(0, box[0] - pad)
    top = max(0, box[1] - pad)
    right = min(image.width, box[2] + pad)
    bottom = min(image.height, box[3] + pad)
    return image.crop((left, top, right, bottom))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("names", nargs="*", help="что генерировать; пусто — всё")
    parser.add_argument("--force", action="store_true", help="перегенерировать существующее")
    parser.add_argument("--list", action="store_true", help="показать задания и выйти")
    args = parser.parse_args()

    if args.list:
        for job in JOBS:
            mark = "сетка" if job.grid else "одна"
            print(f"  {job.name:<12} {mark:<6} {job.prompt[:70]}…")
        return

    settings = get_settings()
    key = settings.llm_api_key
    if not key:
        raise SystemExit("нет LLM_API_KEY в .env")

    OUT.mkdir(parents=True, exist_ok=True)
    wanted = [BY_NAME[n] for n in args.names] if args.names else list(JOBS)
    spent = 0.0

    for job in wanted:
        target = OUT / f"{job.name}.png"
        if target.exists() and not args.force:
            print(f"{job.name}: уже есть, пропускаю (--force чтобы перегенерировать)")
            continue
        print(f"{job.name}: генерирую…")
        blob = generate(job, key, settings.llm_base_url.rstrip("/"))
        if blob is None:
            continue
        target.write_bytes(blob)
        spent += PRICE_HINT
        size = len(blob) // 1024
        if job.grid:
            count = slice_sheet(blob, job)
            print(f"  сохранил {target.name} ({size} КБ), нарезал иконок: {count}")
        else:
            print(f"  сохранил {target.name} ({size} КБ)")

    print(f"\nПотрачено примерно {spent:.0f} ₽ на {int(spent / PRICE_HINT)} генераций")
    print(f"Файлы: {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
