# Деплой «Честного месяца»

Два пути: docker (рекомендуется) и systemd (если docker на сервере нет).
Приложение одно: `python -m app.main` поднимает и телеграм-бота (long
polling), и HTTP API на `API_PORT`.

## Что положить в `.env`

Скопируйте `.env.example` → `.env` и заполните минимум:

| Переменная | Зачем |
|---|---|
| `BOT_TOKEN` | токен от @BotFather, без него бот не стартует |
| `INGEST_API_KEY` | ключ служебной заливки; пусто = эндпоинты выключены |
| `LLM_API_KEY` | ключ провайдера из `LLM_PROVIDER`; пусто = только эвристики |
| `API_PORT` | порт API, он же публикуемый порт контейнера (по умолчанию 8080) |
| `DATABASE_URL` | оставьте `sqlite:///./data/runtime/honest.db` — это путь внутрь тома |
| `MINIAPP_URL` | публичный URL Mini App; пусто = кнопка не показывается |

`.env` читают и docker (`env_file`), и systemd (`EnvironmentFile`), поэтому
формат строгий: `KEY=value`, без `export`, без кавычек вокруг значения.

`ASR_PROVIDER=local` в образе не заработает: локальный whisper — это
необязательная зависимость (`pip install .[asr]`), её в образе нет.
Распознавание голоса по сети (`openrouter`) работает без доустановки.

## Путь 1: docker

Для существующего боевого стенда `grosh.cu3rd.ru` на Windows используйте
`deploy/deploy.ps1` из корня проекта. Он выкладывает один Git-коммит,
проверяет тесты и здоровье контейнера; локальный `.env` и Docker не нужны.
Подробности — в [HOSTING.md](HOSTING.md).

Для нового сервера остаётся общий Bash-скрипт (`home` — алиас из
`~/.ssh/config`):

```bash
./deploy/deploy.sh home
```

Скрипт зальёт проект, проверит наличие серверного `.env`, соберёт образ и
поднимет контейнер. Локальный `.env` не передаётся; для нового сервера
подготовьте отдельный `.env` заранее. Другой каталог на сервере:
`REMOTE_DIR=/srv/honest ./deploy/deploy.sh home`.

Вручную на сервере:

```bash
docker compose up -d --build
```

База лежит в именованном томе `honest-data`, поэтому переживает пересборку
образа и `docker compose down`. Удаляет её только `down -v`.

## Путь 2: systemd (без docker)

```bash
sudo useradd --system --home /opt/honest-month honest
sudo mkdir -p /opt/honest-month && sudo chown honest:honest /opt/honest-month
# залить код в /opt/honest-month (rsync/git), затем:
sudo -u honest python3.11 -m venv /opt/honest-month/.venv
sudo -u honest /opt/honest-month/.venv/bin/pip install /opt/honest-month
sudo install -m 600 -o honest -g honest .env /opt/honest-month/.env
sudo cp /opt/honest-month/deploy/honest-month.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now honest-month
```

Путь к базе в `.env` относительный, он считается от `WorkingDirectory`,
то есть база окажется в `/opt/honest-month/data/runtime/honest.db`.

## Проверить, что живо

```bash
curl -s localhost:8080/healthz          # {"ok":true,...}
```

Ответ показывает провайдера LLM, модель зрения и включена ли заливка.
В docker живость дополнительно проверяет HEALTHCHECK: `docker compose ps`
покажет `healthy` через полминуты после старта.

## Логи

```bash
docker compose logs -f --tail 100       # docker
journalctl -u honest-month -f -n 100    # systemd
```

## Откат

На сервере лежит копия без `.git` (её исключает синхронизация), поэтому
версией управляем с локальной машины:

```bash
git checkout <предыдущий-коммит>
./deploy/deploy.sh home                 # зальёт старый код и пересоберёт образ
```

Образ каждый раз перетирается тегом `latest`, отката «на предыдущий образ»
нет — откатывается код. Погасить сервис немедленно:

```bash
docker compose down                     # том с базой остаётся, данные целы
sudo systemctl stop honest-month        # systemd
```

База не мигрируется автоматически между версиями схемы: перед рискованным
обновлением снимите копию.

```bash
docker compose cp app:/app/data/runtime/honest.db ./honest-backup.db
```
