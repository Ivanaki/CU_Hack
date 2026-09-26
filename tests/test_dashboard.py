"""Локальный пульт принимает только известные действия со своей страницы."""

from __future__ import annotations

import json
from http.server import ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scripts import dashboard


def test_dashboard_rejects_other_origins_and_unknown_commands(monkeypatch):
    with ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler) as server:
        port = server.server_port
        monkeypatch.setattr(dashboard, "PORT", port)
        monkeypatch.setattr(dashboard, "BASE_URL", f"http://127.0.0.1:{port}")
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def send(action: str, origin: str) -> int:
                request = Request(
                    f"http://127.0.0.1:{port}/api/run",
                    data=json.dumps({"id": action}).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "X-Grosh-Panel": "1",
                        "Origin": origin,
                    },
                    method="POST",
                )
                try:
                    with urlopen(request, timeout=3) as response:
                        return response.status
                except HTTPError as error:
                    return error.code

            with patch.object(dashboard.subprocess, "Popen") as popen:
                assert send("tests", "https://another-site.example") == 403
                assert send("arbitrary-command", dashboard.BASE_URL) == 404
                assert send("tests", dashboard.BASE_URL) == 200
                popen.assert_called_once()
                assert popen.call_args.args[0] == dashboard.ACTIONS["tests"]
        finally:
            server.shutdown()
            thread.join(timeout=3)


def test_secret_is_copied_only_from_allowed_local_page(monkeypatch, tmp_path):
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("tB=test-value\n", encoding="utf-8")
    monkeypatch.setattr(dashboard, "SECRET_FILE", secret_file)

    with ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler) as server:
        port = server.server_port
        monkeypatch.setattr(dashboard, "PORT", port)
        monkeypatch.setattr(dashboard, "BASE_URL", f"http://127.0.0.1:{port}")
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def send(origin: str):
                request = Request(
                    f"http://127.0.0.1:{port}/api/secret",
                    data=b'{"id":"copy"}',
                    headers={
                        "Content-Type": "application/json",
                        "X-Grosh-Panel": "1",
                        "Origin": origin,
                    },
                    method="POST",
                )
                try:
                    return urlopen(request, timeout=3)
                except HTTPError as error:
                    return error

            with send("https://another-site.example") as response:
                assert response.status == 403
            with send(dashboard.BASE_URL) as response:
                assert response.status == 200
                assert response.headers["Cache-Control"] == "no-store"
                assert json.load(response) == {"secret": "tB=test-value"}
        finally:
            server.shutdown()
            thread.join(timeout=3)
