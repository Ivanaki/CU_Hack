"""Клиент LLM. OpenRouter или любой OpenAI-совместимый эндпоинт.

Правило, по которому здесь всё устроено: LLM — усилитель, а не несущая
конструкция. Кейс прямо говорит, что языковая модель упрощает разбор
назначений платежей, но не обязательна. Поэтому любой сбой — таймаут,
кончившиеся кредиты, мусор вместо JSON — возвращает None, и вызывающий код
спокойно работает на эвристиках. Ни одно исключение отсюда не вылетает.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


@dataclass(slots=True)
class Usage:
    """Сколько потрачено. Показывается в /debug, чтобы видеть расход кредитов."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLM:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.usage = Usage()

    @property
    def available(self) -> bool:
        if self.settings.llm_provider == "none":
            return False
        if self.settings.llm_provider in {"openrouter", "polza"}:
            return bool(self.settings.llm_api_key)
        return True

    # -- низкий уровень ----------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        if self.settings.llm_provider == "openrouter":
            # OpenRouter просит идентифицировать приложение в рейтингах.
            headers["HTTP-Referer"] = "https://github.com/honest-month"
            headers["X-Title"] = "Honest Month"
        return headers

    def _post(self, payload: dict[str, Any], deadline: float | None = None) -> str | None:
        url = f"{self.settings.llm_base_url.rstrip('/')}/chat/completions"
        last_error: str | None = None
        for _attempt in range(self.settings.llm_max_retries + 1):
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 1:
                last_error = "истёк бюджет времени на разбор"
                break
            try:
                response = httpx.post(
                    url,
                    json=payload,
                    headers=self._headers(),
                    # Ждать дольше общего бюджета бессмысленно: ответ приедет
                    # уже после того, как мы обязаны ответить клиенту.
                    timeout=min(self.settings.llm_timeout_s, left or self.settings.llm_timeout_s),
                )
                if response.status_code >= 400:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                    # 4xx кроме 429 повторять бессмысленно.
                    if response.status_code != 429 and response.status_code < 500:
                        break
                    continue
                data = response.json()
                if "error" in data and not data.get("choices"):
                    last_error = str(data["error"])[:200]
                    break
                choices = data.get("choices") or []
                if not choices:
                    last_error = f"пустой ответ: {str(data)[:200]}"
                    break
                usage = data.get("usage") or {}
                self.usage.calls += 1
                self.usage.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                self.usage.completion_tokens += int(usage.get("completion_tokens") or 0)
                return choices[0].get("message", {}).get("content") or ""
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
        if last_error:
            log.warning("LLM недоступна (%s), работаем на эвристиках", last_error)
        return None

    @staticmethod
    def _extract_json(text: str) -> Any | None:
        """Достаёт JSON даже если модель обернула его в ```json или болтовню."""
        if not text:
            return None
        candidates = [text]
        block = _JSON_BLOCK.search(text)
        if block:
            candidates.insert(0, block.group(1))
        # Наивная обрезка до первой { или [ и последней } или ]
        for opener, closer in (("{", "}"), ("[", "]")):
            start, end = text.find(opener), text.rfind(closer)
            if 0 <= start < end:
                candidates.append(text[start : end + 1])
        for candidate in candidates:
            try:
                return json.loads(candidate.strip())
            except (json.JSONDecodeError, TypeError):
                continue
        return None

    # -- высокий уровень ---------------------------------------------------

    def json(
        self,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
        max_tokens: int = 2000,
        temperature: float = 0.0,
    ) -> Any | None:
        """Текстовый запрос, ожидающий JSON. None означает «не получилось»."""
        if not self.available:
            return None
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        raw = self._post(
            {
                "model": model or self.settings.llm_model_fast,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "response_format": {"type": "json_object"},
            }
        )
        return self._extract_json(raw or "")

    def text(
        self,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
        max_tokens: int = 1200,
        temperature: float = 0.4,
    ) -> str | None:
        if not self.available:
            return None
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        raw = self._post(
            {
                "model": model or self.settings.llm_model_smart,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        return (raw or "").strip() or None

    def vision_json(
        self,
        prompt: str,
        images: list[bytes],
        *,
        system: str = "",
        model: str | None = None,
        max_tokens: int = 4000,
        mime: str = "image/jpeg",
    ) -> Any | None:
        """Разбор скриншота или фото чека. Возвращает JSON или None."""
        if not self.available or not images:
            return None
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for blob in images:
            encoded = base64.b64encode(blob).decode()
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}"},
                }
            )
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})

        # Перебираем модели по цепочке: на демонстрации устойчивость важнее
        # экономии, а провайдер может отвалиться в любой момент.
        chain = [model] if model else self.settings.vision_chain
        # Бюджет общий на всю цепочку, а не на каждую модель: человек ждёт
        # ответа один раз, и ему всё равно, сколько провайдеров мы обошли.
        deadline = time.monotonic() + self.settings.llm_deadline_s
        for candidate in chain:
            if time.monotonic() >= deadline - 1:
                log.warning("бюджет времени на разбор исчерпан, дальше не иду")
                break
            raw = self._post(
                {
                    "model": candidate,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"},
                },
                deadline=deadline,
            )
            parsed = self._extract_json(raw or "")
            if parsed is not None:
                if candidate != chain[0]:
                    log.info("скриншот разобран запасной моделью %s", candidate)
                return parsed
        return None


_default: LLM | None = None


def get_llm() -> LLM:
    global _default
    if _default is None:
        _default = LLM()
    return _default
