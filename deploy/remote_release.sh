#!/usr/bin/env bash
# Выполняется на сервере из каталога распакованного Git-коммита.
set -euo pipefail

sha="${1:-}"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo 'Нужен полный SHA коммита' >&2; exit 2; }
index_hash="${2:-}"
[[ "$index_hash" =~ ^[0-9a-f]{64}$ ]] || { echo 'Нужен SHA256 index.html' >&2; exit 2; }

release_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
expected_dir="$HOME/honest-month-releases/$sha"
[[ "$release_dir" == "$expected_dir" ]] || {
  echo "Неожиданный каталог релиза: $release_dir" >&2; exit 2;
}

prod_dir="$HOME/honest-month"
[[ -f "$prod_dir/.env" && -f "$prod_dir/docker-compose.yml" ]] || {
  echo "Не найдены $prod_dir/.env или docker-compose.yml" >&2; exit 2;
}
[[ -f "$release_dir/docker-compose.yml" && -f "$release_dir/deploy/compose.release.yml" ]] || {
  echo 'В архиве нет конфигурации Docker Compose' >&2; exit 2;
}
actual_hash="$(sha256sum "$release_dir/miniapp/dist/index.html" | cut -d' ' -f1)"
[[ "$actual_hash" == "$index_hash" ]] || {
  echo 'Переданный index.html не совпадает с проверенным коммитом' >&2; exit 2;
}

# Порт закреплён за этим сервисом на общей машине. Не позволяем новому
# релизу случайно занять другой порт или запустить контейнер с чужой базой.
port="$(sed -n 's/^API_PORT=//p' "$prod_dir/.env" | head -n 1)"
[[ "$port" == 8091 ]] || { echo "Ожидался API_PORT=8091, найден: $port" >&2; exit 2; }
project="$(docker inspect honest-month --format '{{ index .Config.Labels "com.docker.compose.project" }}' 2>/dev/null || true)"
[[ "$project" == 'honest-month' ]] || {
  echo "Контейнер honest-month не принадлежит проекту honest-month: $project" >&2; exit 2;
}

# Compose читает env_file относительно файла релиза. Ссылка ведёт на
# существующий боевой файл; git archive никогда не содержит .env.
if [[ -e "$release_dir/.env" || -L "$release_dir/.env" ]]; then
  [[ -L "$release_dir/.env" && "$(readlink "$release_dir/.env")" == "$prod_dir/.env" ]] || {
    echo 'В каталоге релиза уже есть другой .env' >&2; exit 2;
  }
else
  ln -s "$prod_dir/.env" "$release_dir/.env"
fi

docker compose version >/dev/null

# SSH может оборваться после успешного запуска. Повтор того же релиза
# подтверждает уже работающий образ, не перезапуская контейнер.
if existing_image="$(docker image inspect "honest-month:$sha" --format '{{.Id}}' 2>/dev/null)"; then
  running_image="$(docker inspect honest-month --format '{{.Image}}' 2>/dev/null || true)"
  health="$(docker inspect honest-month --format '{{.State.Health.Status}}' 2>/dev/null || true)"
  if [[ "$running_image" == "$existing_image" && "$health" == healthy ]] && \
     curl -fsS --max-time 3 http://127.0.0.1:8091/healthz >/dev/null && \
     served_hash="$(curl -fsS --max-time 3 http://127.0.0.1:8091/ | sha256sum | cut -d' ' -f1)" && \
     [[ "$served_hash" == "$index_hash" ]]; then
    printf '%s\n' "$sha" > "$prod_dir/.deployed-sha"
    echo "Релиз $sha уже работает на порту 8091"
    exit 0
  fi
fi

previous_image="$(docker inspect honest-month --format '{{.Image}}')"
rollback_tag="rollback-$(date +%Y%m%d%H%M%S)"
docker tag "$previous_image" "honest-month:$rollback_tag"

echo "Собираю образ для $sha"
docker build --label "org.opencontainers.image.revision=$sha" \
  -t "honest-month:$sha" "$release_dir"
expected_image="$(docker image inspect "honest-month:$sha" --format '{{.Id}}')"

compose_release() {
  DEPLOY_SHA="$sha" docker compose --env-file "$prod_dir/.env" -p honest-month \
    -f "$release_dir/docker-compose.yml" \
    -f "$release_dir/deploy/compose.release.yml" "$@"
}

rollback() {
  echo "Релиз $sha не прошёл проверку; возвращаю прежний образ" >&2
  DEPLOY_SHA="$rollback_tag" docker compose --env-file "$prod_dir/.env" -p honest-month \
    -f "$prod_dir/docker-compose.yml" \
    -f "$release_dir/deploy/compose.release.yml" up -d --no-build app
}

if ! compose_release up -d --no-build app; then
  rollback
  exit 1
fi

healthy=false
for _ in $(seq 1 45); do
  running_image="$(docker inspect honest-month --format '{{.Image}}' 2>/dev/null || true)"
  health="$(docker inspect honest-month --format '{{.State.Health.Status}}' 2>/dev/null || true)"
  if [[ "$running_image" == "$expected_image" && "$health" == healthy ]] && \
     curl -fsS --max-time 3 http://127.0.0.1:8091/healthz >/dev/null; then
    if served_hash="$(curl -fsS --max-time 3 http://127.0.0.1:8091/ | sha256sum | cut -d' ' -f1)" && \
       [[ "$served_hash" == "$index_hash" ]]; then
      healthy=true
      break
    fi
  fi
  sleep 2
done

if [[ "$healthy" != true ]]; then
  rollback
  exit 1
fi

printf '%s\n' "$sha" > "$prod_dir/.deployed-sha"
compose_release ps app
echo "Релиз $sha работает на порту 8091; откат: honest-month:$rollback_tag"
