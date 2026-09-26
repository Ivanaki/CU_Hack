"""Локальная браузерная панель управления проектом.

Запуск: python -m scripts.dashboard. Слушает только 127.0.0.1:8092.
Веб-страница может запускать лишь команды из ACTIONS; произвольные команды
и параметры из браузера не принимаются.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.request
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Timer

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "dashboard"
SECRET_FILE = ROOT / "data" / "runtime" / "dashboard-secret.txt"
HOST = "127.0.0.1"
PORT = 8092
BASE_URL = f"http://{HOST}:{PORT}"
PS = ["powershell.exe", "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass"]

ACTIONS = {
    "local": [*PS, "-File", str(ROOT / "scripts" / "local.ps1")],
    "stop": [*PS, "-File", str(ROOT / "scripts" / "local.ps1"), "-Stop"],
    "reset": [*PS, "-File", str(ROOT / "scripts" / "local.ps1"), "-Reset"],
    "tests": [*PS, "-Command", "& .\\.venv\\Scripts\\python.exe -m pytest -q"],
    "viewport": [*PS, "-Command", "node --test tests\\miniapp_viewport.test.cjs"],
    "dryrun": [*PS, "-File", str(ROOT / "deploy" / "deploy.ps1"), "-Ref", "HEAD", "-DryRun"],
    "deploy": [*PS, "-File", str(ROOT / "deploy" / "deploy.ps1"), "-MaxAttempts", "5"],
    "ssh": [*PS, "-Command", "ssh egor@87.120.84.226"],
}

FOLDERS = {
    "root": ROOT,
    "database": ROOT / "data" / "runtime",
    "uploads": ROOT / "data" / "uploads-local",
}


def project_status() -> dict:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8081/healthz", timeout=1) as response:
            alive = response.status == 200
    except (OSError, TimeoutError):
        alive = False

    def git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args], cwd=ROOT, capture_output=True, text=True,
                timeout=3, check=True, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return result.stdout.strip()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None

    branch = git("branch", "--show-current")
    changes = git("status", "--porcelain")
    return {
        "alive": alive,
        "branch": branch or "неизвестно",
        "changes": None if changes is None else len(changes.splitlines()),
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PAGE), **kwargs)

    def _allowed_host(self) -> bool:
        return self.headers.get("Host") == f"{HOST}:{PORT}"

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if not self._allowed_host():
            self.send_error(403)
            return
        route = self.path.split("?", 1)[0]
        if route == "/api/status":
            self._json(200, project_status())
            return
        if route == "/healthz":
            self._json(200, {"dashboard": True})
            return
        if route == "/":
            self.path = "/index.html"
        elif route not in {"/index.html", "/styles.css", "/app.js", "/catalog.js"}:
            self.send_error(404)
            return
        super().do_GET()

    def do_POST(self) -> None:
        if not self._allowed_host() or self.headers.get("Origin") != BASE_URL:
            self._json(403, {"error": "Недопустимый источник запроса"})
            return
        valid_header = self.headers.get("X-Grosh-Panel") == "1"
        valid_type = self.headers.get("Content-Type", "").startswith("application/json")
        if not valid_header or not valid_type:
            self._json(403, {"error": "Недопустимый запрос"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 1024:
                raise ValueError("Неверный размер запроса")
            body = json.loads(self.rfile.read(length))
            target = body["id"]
            if not isinstance(target, str):
                raise ValueError("Неверная команда")
        except (ValueError, KeyError, json.JSONDecodeError):
            self._json(400, {"error": "Неверный запрос"})
            return

        try:
            if self.path == "/api/secret" and target == "copy":
                try:
                    secret = SECRET_FILE.read_text(encoding="utf-8-sig").rstrip("\r\n")
                except FileNotFoundError:
                    self._json(404, {"error": "Секретная строка не найдена"})
                    return
                self._json(200, {"secret": secret})
                return
            elif self.path == "/api/run" and target in ACTIONS:
                subprocess.Popen(
                    ACTIONS[target], cwd=ROOT, creationflags=subprocess.CREATE_NEW_CONSOLE
                )
            elif self.path == "/api/open" and target == "app":
                webbrowser.open("http://127.0.0.1:8081/?tg_id=1")
            elif self.path == "/api/open" and target in FOLDERS:
                FOLDERS[target].mkdir(parents=True, exist_ok=True)
                os.startfile(FOLDERS[target])
            else:
                self._json(404, {"error": "Неизвестное действие"})
                return
        except OSError as exc:
            self._json(500, {"error": str(exc)})
            return
        self._json(200, {"ok": True})

    def log_message(self, _format: str, *_args) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        with urllib.request.urlopen(f"{BASE_URL}/healthz", timeout=1) as response:
            if json.load(response).get("dashboard"):
                if not args.no_browser:
                    webbrowser.open(BASE_URL)
                return
    except (OSError, TimeoutError, ValueError):
        pass

    with ThreadingHTTPServer((HOST, PORT), DashboardHandler) as server:
        print(f"Пульт Грош: {BASE_URL}")
        print("Закройте это окно или нажмите Ctrl+C, чтобы остановить пульт.")
        if not args.no_browser:
            Timer(0.5, lambda: webbrowser.open(BASE_URL)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
