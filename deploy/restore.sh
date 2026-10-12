#!/usr/bin/env bash
# Restore a dump into the running stack's database (DESTROYS current data). Usage: restore.sh <file.dump>
set -euo pipefail
cd "$(dirname "$0")"
f="${1:?usage: restore.sh <file.dump>}"
docker compose stop api worker web
docker compose exec -T db dropdb -U pm --if-exists pm
docker compose exec -T db createdb -U pm pm
docker compose exec -T db pg_restore -U pm -d pm --no-owner < "$f"
docker compose start api worker web
echo "restored $f"
