#!/bin/sh
set -eu

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if [ "$#" -gt 0 ]; then
  DUMP_FILE="$1"
else
  DUMP_FILE="${DUMP_FILE:-}"
fi

if [ -z "$DUMP_FILE" ]; then
  echo "usage: docker/restore_database.sh backups/letpub_crawler_<timestamp>.dump" >&2
  exit 2
fi

if [ ! -f "$DUMP_FILE" ]; then
  echo "backup not found: $DUMP_FILE" >&2
  exit 2
fi

docker compose up -d db >/dev/null

docker compose exec -T db sh -lc '
i=0
until pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -ge 60 ]; then
    echo "database is not ready" >&2
    exit 1
  fi
  sleep 2
done
'

docker compose exec -T db sh -lc '
pg_restore \
  --clean \
  --if-exists \
  --no-owner \
  --no-acl \
  -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB"
' < "$DUMP_FILE"

docker compose exec -T db sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
select 'categories=' || count(*) from categories;
select 'journals=' || count(*) from journals;
select 'comments=' || count(*) from comments;
select 'crawl_tasks=' || count(*) from crawl_tasks;
select 'cookie_pool=' || count(*) from cookie_pool;
select 'accounts=' || count(*) from accounts;
SQL

printf 'restored backup: %s\n' "$DUMP_FILE"
