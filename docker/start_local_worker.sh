#!/bin/sh
set -eu

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$PROJECT_DIR"

scale="${1:-${LOCAL_WORKER_SCALE:-1}}"

if [ "$scale" != "1" ]; then
  # A fixed worker ID would make scaled local workers overwrite each other in the worker registry.
  export LOCAL_WORKER_ID=""
  export CRAWLER_WORKER_ID=""
fi

docker compose --profile worker up -d --build --scale "worker=${scale}" worker

printf 'local worker scale: %s\n' "$scale"
docker compose ps
