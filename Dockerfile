# Образ «Честного месяца»: бот и HTTP API в одном процессе.
# Две стадии, потому что компилятор нужен только на сборке: наружу уезжает
# готовый venv, а build-essential остаётся в выброшенном слое.

FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Нужен на случай зависимости без готового колеса под архитектуру сервера
# (обычно arm64) — на amd64 почти всегда не задействуется.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

# Отдельный venv, а не --user: его копируют в рантайм одной строкой.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src

# Зависимости ставим ДО кода: этот слой пересобирается только при правке
# pyproject.toml, правка кода переиспользует кэш. Список тянем из самого
# pyproject, чтобы не заводить второй источник правды в requirements.txt.
COPY pyproject.toml ./
RUN python -c "import pathlib, tomllib; print(chr(10).join(tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8'))['project']['dependencies']))" > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt

# Сам пакет — уже без зависимостей, они стоят слоем выше.
COPY app ./app
RUN pip install --no-deps .


FROM python:3.11-slim AS runtime

# В slim-образе нет базы часовых поясов, без неё TZ=Europe/Moscow молча
# превращается в UTC — и дайджест «в 21:00» уезжает на три часа.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    API_PORT=8080

RUN useradd --system --create-home --uid 10001 honest

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=honest:honest pyproject.toml ./
COPY --chown=honest:honest app ./app
# Mini App отдаётся тем же процессом, что и API: на хакатоне второй
# веб-сервер и второй домен — лишняя точка отказа.
COPY --chown=honest:honest miniapp/dist ./miniapp/dist
# Демонстрационный набор и скрипты нужны прямо на сервере: показывать
# продукт приходится там, где он развёрнут.
COPY --chown=honest:honest data/demo ./data/demo
COPY --chown=honest:honest scripts ./scripts

# Код лежит в /app и WORKDIR=/app, поэтому app.config.ROOT == /app: путь
# sqlite:///./data/runtime/honest.db из .env указывает ровно в том ниже.
# Права выставляем до VOLUME — иначе том унаследует root и запись упадёт.
RUN mkdir -p /app/data/runtime /app/data/uploads && chown -R honest:honest /app/data
VOLUME ["/app/data/runtime", "/app/data/uploads"]

USER honest
EXPOSE 8080

# Живость проверяем тем же эндпоинтом, что и человек снаружи. curl в slim нет,
# поэтому python: urlopen сам бросает исключение на не-2xx и на таймаут.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('API_PORT', '8080') + '/healthz', timeout=3)" || exit 1

CMD ["python", "-m", "app.main"]
