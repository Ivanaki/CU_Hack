"""Сквозная проверка боевого сервера: дёргает каждую ручку и сверяет ответы.

Не «тесты локально зелёные», а «оно работает там, где будет демонстрация».
Проверяет и то, что должно работать, и то, что должно быть закрыто:
чужие данные, отсутствие подписи, обход пути.

    python -m scripts.smoke                     # локально, http://127.0.0.1:8080
    python -m scripts.smoke --base <URL>        # боевой адрес
    python -m scripts.smoke --tg-id 1058389184  # от имени конкретного человека
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, FAIL, SKIP = "ок", "ПРОВАЛ", "пропуск"
results: list[tuple[str, str, str]] = []


def check(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    mark = {OK: "  ok ", FAIL: " ПРОВ", SKIP: " проп"}[status]
    print(f"{mark}  {name:<46} {detail}")


def init_data(tg_id: int, token: str) -> str:
    """Подписанная initData — ровно то, что присылает Telegram Mini App."""
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAsmoke",
        "user": json.dumps({"id": tg_id, "first_name": "Проверка"}, ensure_ascii=False),
    }
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def call(
    base: str,
    path: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    body: bytes | None = None,
    content_type: str | None = None,
    timeout: int = 60,
) -> tuple[int, object]:
    request = urllib.request.Request(base + path, data=body, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    if content_type:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            try:
                return response.status, json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return response.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return exc.code, raw
    except Exception as exc:  # noqa: BLE001 — проверка не должна падать целиком
        return 0, f"{type(exc).__name__}: {exc}"


def multipart(
    fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]
) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    crlf = "\r\n"
    parts: list[bytes] = []
    for key, value in fields.items():
        head = f'Content-Disposition: form-data; name=\"{key}\"'
        parts.append(f"--{boundary}{crlf}{head}{crlf}{crlf}{value}{crlf}".encode())
    for key, (filename, data, ctype) in files.items():
        head = (
            f'Content-Disposition: form-data; name=\"{key}\"; filename=\"{filename}\"'
        )
        parts.append(
            f"--{boundary}{crlf}{head}{crlf}Content-Type: {ctype}{crlf}{crlf}".encode()
            + data
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a"
    "49444154789c6360000002000100ffff03000006000557bfabd40000000049454e44ae426082"
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument("--tg-id", type=int, default=1058389184)
    parser.add_argument("--period", default="2026-09")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    env = (ROOT / ".env").read_text(encoding="utf-8") if (ROOT / ".env").exists() else ""
    token = (re.search(r"BOT_TOKEN=(\S+)", env) or [None, ""])[1]
    service_key = (re.search(r"INGEST_API_KEY=(\S+)", env) or [None, ""])[1]
    if not token:
        raise SystemExit("нет BOT_TOKEN в .env — без него не собрать подпись")

    auth = {"X-Telegram-Init-Data": init_data(args.tg_id, token)}
    service = {"X-API-Key": service_key}

    print(f"Проверяю {base}\n")

    # --- живость и статика ------------------------------------------------
    status, data = call(base, "/healthz")
    check("healthz", OK if status == 200 else FAIL, str(data)[:70])

    status, data = call(base, "/")
    size = len(data) if isinstance(data, bytes) else 0
    check("Mini App отдаётся", OK if status == 200 and size > 5000 else FAIL, f"{size} б")

    status, _ = call(base, "/docs")
    check("Swagger", OK if status == 200 else FAIL, f"HTTP {status}")

    # --- Mini App: чтение --------------------------------------------------
    status, summary = call(base, f"/api/v1/me/summary?period={args.period}", headers=auth)
    if status != 200 or not isinstance(summary, dict):
        check("сводка", FAIL, str(summary)[:70])
        summary = {}
    else:
        check(
            "сводка", OK,
            f"{summary['spend']['text']}, "
            f"осознанность {summary['awareness']['percent']}%",
        )

    if summary:
        spend = summary["spend"]["kopecks"]
        weeks = sum(w["amount"]["kopecks"] for w in summary["weeks"])
        cats = sum(c["amount"]["kopecks"] for c in summary["categories"])
        split = summary["essential"]["kopecks"] + summary["optional"]["kopecks"]
        check("недели == месяц", OK if weeks == spend else FAIL, f"{weeks} vs {spend}")
        check("категории == месяц", OK if cats == spend else FAIL, f"{cats} vs {spend}")
        check("обяз+необяз == месяц", OK if split == spend else FAIL, f"{split} vs {spend}")
        forecast = summary.get("forecast")
        check(
            "прогноз",
            OK if forecast and forecast.get("projected") else SKIP,
            forecast["projected"]["text"] if forecast else "нет",
        )
        wallets = summary.get("wallets") or []
        check(
            "кошелёк маркетплейса",
            OK if wallets else SKIP,
            f"{wallets[0]['name']}: {wallets[0]['topped_up']['text']}" if wallets else "нет",
        )
        check(
            "советы", OK if summary.get("insights") else SKIP,
            f"{len(summary.get('insights', []))} шт.",
        )

    status, data = call(base, "/api/v1/me/checkup", headers=auth)
    ok = status == 200 and isinstance(data, dict) and data.get("ok")
    check("проверка сходимости", OK if ok else FAIL, str(data)[:70] if not ok else "все инварианты")

    status, questions = call(base, "/api/v1/me/questions", headers=auth)
    check(
        "очередь вопросов",
        OK if status == 200 and isinstance(questions, list) else FAIL,
        f"{len(questions)} шт." if isinstance(questions, list) else str(questions)[:60],
    )

    status, ops = call(base, f"/api/v1/me/operations?period={args.period}&limit=5", headers=auth)
    check(
        "операции", OK if status == 200 and isinstance(ops, list) and ops else FAIL,
        f"{len(ops)} шт." if isinstance(ops, list) else str(ops)[:60],
    )

    status, game = call(base, "/api/v1/me/game", headers=auth)
    if status == 404:
        check("геймификация", SKIP, "ещё не задеплоена")
    elif status == 200 and isinstance(game, dict):
        check("геймификация", OK,
              f"уровень {game.get('level', {}).get('number')}, "
              f"квестов {len(game.get('quests', []))}, карта {len(game.get('map', []))}")
    else:
        check("геймификация", FAIL, str(game)[:70])

    # --- Mini App: запись --------------------------------------------------
    body = json.dumps({"text": "проверка связи 1"}).encode()
    status, data = call(base, "/api/v1/me/expenses", method="POST", headers=auth,
                        body=body, content_type="application/json")
    check("быстрый ввод траты", OK if status == 200 and data.get("ok") else FAIL, str(data)[:70])

    payload, ctype = multipart({}, {"files": ("t.png", PNG_1PX, "image/png")})
    status, data = call(base, "/api/v1/me/images", method="POST", headers=auth,
                        body=payload, content_type=ctype, timeout=120)
    # Пустая картинка не должна разобраться — важно, что ручка отвечает осмысленно.
    check("загрузка картинки", OK if status in (200, 400) else FAIL, f"HTTP {status}")

    # --- служебная заливка -------------------------------------------------
    status, users = call(base, "/api/v1/ingest/users", headers=service)
    check(
        "служебный список", OK if status == 200 else FAIL,
        f"{len(users)} польз." if isinstance(users, list) else str(users)[:50],
    )

    # --- загрузка файлов ---------------------------------------------------
    status, data = call(base, "/upload", method="POST",
                        body=PNG_1PX, content_type="image/png",
                        headers={"X-Filename": "smoke.png"}, timeout=120)
    stored = data.get("stored_as") if isinstance(data, dict) else None
    if status == 404:
        check("POST /upload (тело)", SKIP, "ещё не задеплоено")
    else:
        check("POST /upload (тело)", OK if status == 200 and stored else FAIL, str(data)[:70])

    if stored:
        payload, ctype = multipart({}, {"file": ("m.png", PNG_1PX, "image/png")})
        status, data = call(base, "/upload", method="POST", body=payload, content_type=ctype)
        check("POST /upload (multipart)", OK if status == 200 else FAIL, f"HTTP {status}")

        status, data = call(base, "/files")
        found = isinstance(data, dict) and any(f["name"] == stored for f in data.get("files", []))
        check(
            "GET /files", OK if found else FAIL,
            f"{len(data.get('files', []))} файлов" if isinstance(data, dict) else "",
        )

        if isinstance(data, dict) and data.get("files"):
            sample_url = data["files"][0].get("url", "")
            usable = sample_url.startswith("https://") or "localhost" not in sample_url
            check(
                "ссылка в /files пригодна снаружи",
                OK if usable else FAIL,
                sample_url[:60],
            )

        status, raw = call(base, f"/files/{stored}")
        check(
            "GET /files/{name}", OK if status == 200 and isinstance(raw, bytes) else FAIL,
            f"{len(raw) if isinstance(raw, bytes) else 0} б",
        )

        for attack in ("../../.env", "..%2f..%2f.env", "%2e%2e%2f.env"):
            status, _ = call(base, f"/files/{attack}")
            check(
                f"обход пути {attack[:18]}",
                OK if status in (400, 403, 404, 422) else FAIL,
                f"HTTP {status}",
            )

    # --- что должно быть закрыто -------------------------------------------
    status, _ = call(base, "/api/v1/me/summary")
    check("без подписи не пускает", OK if status == 401 else FAIL, f"HTTP {status}")

    bad = init_data(args.tg_id, "999:НЕВЕРНЫЙ")
    status, _ = call(base, "/api/v1/me/summary", headers={"X-Telegram-Init-Data": bad})
    check("подделка подписи отвергнута", OK if status == 401 else FAIL, f"HTTP {status}")

    status, _ = call(base, "/api/v1/ingest/users")
    check("служебный API без ключа закрыт", OK if status == 401 else FAIL, f"HTTP {status}")

    other = {"X-Telegram-Init-Data": init_data(999_000_777, token)}
    status, data = call(base, f"/api/v1/me/summary?period={args.period}", headers=other)
    empty = status == 200 and isinstance(data, dict) and data["spend"]["kopecks"] == 0
    check(
        "чужие данные не видны", OK if empty else FAIL,
        "пустая сводка" if empty else str(data)[:50],
    )

    # --- итог --------------------------------------------------------------
    failed = [r for r in results if r[1] == FAIL]
    skipped = [r for r in results if r[1] == SKIP]
    print(f"\nПроверок: {len(results)}  ок: {len(results) - len(failed) - len(skipped)}  "
          f"пропущено: {len(skipped)}  провалов: {len(failed)}")
    if failed:
        print("\nНе прошло:")
        for name, _, detail in failed:
            print(f"  {name}: {detail}")
        sys.exit(1)


if __name__ == "__main__":
    main()
