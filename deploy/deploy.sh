#!/usr/bin/env bash
# Деплой «Честного месяца» на сервер по ssh-алиасу из ~/.ssh/config.
#
#   ./deploy/deploy.sh                      # алиас home, каталог ~/honest-month
#   ./deploy/deploy.sh myserver             # другой алиас
#   REMOTE_DIR=/srv/honest ./deploy/deploy.sh home
#
# Скрипт идемпотентен: гоняйте сколько угодно раз, .env и база на сервере
# не пострадают.

set -euo pipefail

HOST="${1:-home}"
# Относительный путь раскрывается в домашний каталог ssh-пользователя.
REMOTE_DIR="${REMOTE_DIR:-honest-month}"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# То, что на сервере жить не должно: мусор, локальная база и .env
# (его кладём отдельно и только если его там ещё нет).
EXCLUDES=(
  .git .venv venv __pycache__ '*.pyc' .pytest_cache .ruff_cache
  data/runtime data/uploads '*.db' '*.sqlite3' .env .idea .vscode
)

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
fail() { printf '\033[1;31mОШИБКА: %s\033[0m\n' "$*" >&2; exit 1; }

[[ -f "$LOCAL_DIR/.env" ]] || fail "нет $LOCAL_DIR/.env — скопируйте .env.example и заполните ключи"

step "Проверяю сервер $HOST"
ssh "$HOST" 'echo "    подключился: $(hostname)"' || fail "не достучался до $HOST (проверьте ~/.ssh/config)"
ssh "$HOST" 'command -v docker >/dev/null' \
  || fail "на $HOST нет docker. Поставьте его или разворачивайте через systemd — см. deploy/README.md"

# На старых серверах compose живёт отдельным бинарником, а не плагином.
COMPOSE="$(ssh "$HOST" 'if docker compose version >/dev/null 2>&1; then echo "docker compose"; elif command -v docker-compose >/dev/null 2>&1; then echo "docker-compose"; fi')"
[[ -n "$COMPOSE" ]] || fail "на $HOST нет docker compose (ни плагина, ни docker-compose)"
info "compose: $COMPOSE"

step "Готовлю каталог $REMOTE_DIR"
ssh "$HOST" "mkdir -p '$REMOTE_DIR'"

step "Синхронизирую проект"
if command -v rsync >/dev/null 2>&1; then
  rsync_args=()
  for pattern in "${EXCLUDES[@]}"; do rsync_args+=(--exclude "$pattern"); done
  rsync -az --delete "${rsync_args[@]}" "$LOCAL_DIR/" "$HOST:$REMOTE_DIR/"
  info "rsync: лишние файлы на сервере удалены"
else
  # Запасной путь для машин без rsync (обычный случай на Windows).
  # Важно: tar не умеет --delete, удалённые локально файлы на сервере
  # останутся. Если это мешает — снесите каталог на сервере руками.
  info "rsync не найден, заливаю через tar | ssh (без удаления лишнего)"
  tar_args=()
  for pattern in "${EXCLUDES[@]}"; do tar_args+=("--exclude=$pattern"); done
  tar czf - -C "$LOCAL_DIR" "${tar_args[@]}" . | ssh "$HOST" "tar xzf - -C '$REMOTE_DIR'"
fi

step "Проверяю .env на сервере"
if ssh "$HOST" "test -f '$REMOTE_DIR/.env'"; then
  info ".env уже есть — не трогаю (правьте на сервере)"
else
  scp "$LOCAL_DIR/.env" "$HOST:$REMOTE_DIR/.env"
  ssh "$HOST" "chmod 600 '$REMOTE_DIR/.env'"
  info ".env скопирован с локальной машины"
fi

step "Собираю и запускаю контейнер"
ssh "$HOST" "cd '$REMOTE_DIR' && $COMPOSE up -d --build"

step "Состояние сервиса"
ssh "$HOST" "cd '$REMOTE_DIR' && $COMPOSE ps"

step "Последние 30 строк логов"
ssh "$HOST" "cd '$REMOTE_DIR' && $COMPOSE logs --tail 30"

step "Готово"
info "логи вживую:  ssh $HOST 'cd $REMOTE_DIR && $COMPOSE logs -f'"
info "проверка:     ssh $HOST 'curl -sf localhost:8080/healthz'"
