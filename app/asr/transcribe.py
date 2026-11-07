"""Распознавание голосовых.

Голос нужен ровно для одного: наличных, которых нет ни в какой выписке.
Поэтому распознавание необязательно — при любом сбое возвращается None, и
бот спокойно просит написать текстом.

Модели выбраны замером на русской речи, а не по названию:
    ai-sage/gigaam-v3          1.5 с, «Кофе 300 ₽» — понимает валюту
    openai/whisper-large-v3    2.2 с, «Кофе 300 рублей»
Первая модель основная, остальные — запасные на случай отказа провайдера.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)
_local_model = None


def transcribe(audio: bytes, *, filename: str = "voice.ogg") -> str | None:
    settings = get_settings()
    if settings.asr_provider == "none" or not audio:
        return None
    if settings.asr_provider == "local":
        return _local(audio)
    return _remote(audio, filename=filename)


def _remote(audio: bytes, *, filename: str) -> str | None:
    """OpenAI-совместимый /audio/transcriptions с перебором моделей."""
    settings = get_settings()
    key = settings.asr_api_key or settings.llm_api_key
    if not key:
        log.warning("нет ключа для распознавания речи")
        return None

    url = f"{settings.llm_base_url.rstrip('/')}/audio/transcriptions"
    mime = "audio/ogg" if filename.endswith((".ogg", ".oga")) else "audio/mpeg"
    last_error = ""

    for model in settings.asr_chain:
        try:
            response = httpx.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (filename, audio, mime)},
                data={"model": model, "language": "ru"},
                timeout=settings.llm_timeout_s,
            )
            if response.status_code >= 400:
                last_error = f"{model}: HTTP {response.status_code} {response.text[:120]}"
                continue
            text = (response.json().get("text") or "").strip()
            if text:
                if model != settings.asr_chain[0]:
                    log.info("голос распознан запасной моделью %s", model)
                return text
            last_error = f"{model}: пустой ответ"
        except (httpx.HTTPError, ValueError) as exc:
            last_error = f"{model}: {type(exc).__name__} {exc}"

    log.warning("распознать голос не удалось (%s)", last_error)
    return None


def _local(audio: bytes) -> str | None:
    """faster-whisper на CPU. Ставится отдельно: pip install .[asr]"""
    global _local_model
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415
    except ImportError:
        log.warning("faster-whisper не установлен, голос не распознаётся")
        return None

    settings = get_settings()
    if _local_model is None:
        # Локально asr_model содержит размер модели, а не имя из каталога.
        size = settings.asr_model if "/" not in settings.asr_model else "small"
        _local_model = WhisperModel(size, device="cpu", compute_type="int8")

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as handle:
        handle.write(audio)
        path = Path(handle.name)
    try:
        segments, _ = _local_model.transcribe(str(path), language="ru")
        return " ".join(segment.text.strip() for segment in segments).strip() or None
    except Exception as exc:  # noqa: BLE001 — распознавание не должно ронять бота
        log.warning("локальное распознавание не удалось: %s", exc)
        return None
    finally:
        path.unlink(missing_ok=True)
