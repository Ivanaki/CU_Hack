"""Конфигурация. Всё, что может отличаться между машинами, живёт здесь."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    bot_token: str = ""
    miniapp_url: str = ""

    # Ключ для служебного API: по нему внешний сервис заливает скриншоты
    # пользователей. Пустой ключ означает, что служебные эндпоинты выключены.
    ingest_api_key: str = ""
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    database_url: str = "sqlite:///./data/runtime/honest.db"

    # --- Загрузки с шортката iPhone ---
    # Каталог рядом с базой и в своём томе docker: файлы пользователя обязаны
    # пережить пересборку образа ровно так же, как его операции.
    upload_dir: str = "./data/uploads"
    max_upload_mb: int = 20
    # Внешний адрес сервиса для ссылок на файлы. Нужен настройкой, а не из
    # запроса: снаружи приходят по https через туннель, а внутрь запрос
    # попадает по http — ссылка из request.base_url в браузере не откроется.
    public_base_url: str = ""
    # Пусто — приём файлов открыт, как в ТЗ шортката. Непустое значение
    # включает проверку X-API-Key на запись и на список файлов.
    upload_api_key: str = ""

    # LLM. openrouter — один ключ и на текст, и на зрение; local — любой
    # OpenAI-совместимый эндпоинт (ollama, llama.cpp); none — только эвристики.
    llm_provider: Literal["polza", "openrouter", "local", "none"] = "none"
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_timeout_s: int = 60
    # Три роли, чтобы не платить за мощную модель там, где хватает дешёвой.
    # Модель для зрения выбрана замером на эталонном скриншоте
    # (scripts/bench_vision.py), а не по названию: 8 из 8 операций за 6 секунд.
    llm_model_fast: str = "google/gemini-3.5-flash-lite"
    llm_model_vision: str = "google/gemini-3.5-flash-lite"
    llm_model_smart: str = "anthropic/claude-sonnet-5"
    # Запасные модели зрения: на демонстрации важнее устойчивость, чем цена.
    # gigachat хранит данные в РФ — переключение сюда закрывает вопрос 152-ФЗ
    # без переписывания кода, точность на замере такая же.
    llm_vision_fallbacks: str = "qwen/qwen3-vl-30b-a3b-instruct,anthropic/claude-haiku-4.5"
    llm_max_retries: int = 2

    @property
    def vision_chain(self) -> list[str]:
        chain = [self.llm_model_vision]
        chain += [m.strip() for m in self.llm_vision_fallbacks.split(",") if m.strip()]
        return list(dict.fromkeys(chain))

    # Распознавание речи. remote — тот же OpenAI-совместимый эндпоинт, что и
    # у LLM; local — faster-whisper на CPU; none — голос выключен.
    asr_provider: Literal["remote", "local", "none"] = "none"
    # Выбрано замером на русской речи: gigaam-v3 быстрее всех и сразу пишет
    # «300 ₽» вместо «триста рублей», whisper — надёжный запасной.
    asr_model: str = "ai-sage/gigaam-v3"
    asr_fallbacks: str = "openai/whisper-large-v3-turbo,openai/whisper-large-v3"
    asr_api_key: str = ""

    @property
    def asr_chain(self) -> list[str]:
        chain = [self.asr_model]
        chain += [m.strip() for m in self.asr_fallbacks.split(",") if m.strip()]
        return list(dict.fromkeys(chain))

    tz: str = "Europe/Moscow"
    digest_hour: int = 21
    weekly_summary_weekday: int = 6  # 0=пн, 6=вс

    # Политики классификации — вынесены в конфиг, потому что это продуктовые
    # решения, а не константы кода: их защищают на питче и могут крутить.
    shared_bill_deadline_days: int = 30
    debt_match_window_days: int = 90
    refund_match_window_days: int = 60
    cash_unallocated_after_days: int = 14

    # Окно склейки перевода между своими счетами.
    self_transfer_window_hours: int = 36
    # Допустимое расхождение сумм внутри перевода (комиссия), доля и абсолют.
    self_transfer_fee_ratio: float = 0.02
    self_transfer_fee_abs: int = 10000  # копейки

    @property
    def db_path(self) -> Path | None:
        """Путь к файлу SQLite, если используется он (нужно для mkdir)."""
        if not self.database_url.startswith("sqlite"):
            return None
        raw = self.database_url.split("///", 1)[-1]
        return (ROOT / raw) if not Path(raw).is_absolute() else Path(raw)

    @property
    def upload_path(self) -> Path:
        """Каталог загрузок. Относительный путь считается от корня проекта,
        то есть в контейнере — от /app, куда примонтирован том."""
        raw = Path(self.upload_dir)
        return raw if raw.is_absolute() else (ROOT / raw)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
