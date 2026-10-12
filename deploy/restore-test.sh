#!/usr/bin/env bash
# Monthly restore test: restore the newest nightly dump into a throwaway postgres container
# (no ports, no networks, never the live database) and sanity-check it. Prints one line, exits 1 on failure.
# Cron (first Sunday of the month; cron ORs day-of-month and day-of-week, hence the date test):
#   30 3 * * 0 [ "$(date +\%d)" -le 07 ] && /bin/bash $HOME/portfolio-manager/deploy/restore-test.sh >> $HOME/backups/portfolio-manager/restore-test.log 2>&1
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
dest="${PM_BACKUP_DIR:-$HOME/backups/portfolio-manager}"
name="pm-restore-test-$$-$(date +%s)"
dump_name="none"
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
fail() {
    echo "$(date -Is) restore-test FAILED $dump_name: $1"
    exit 1
}

dump=$(ls -1t "$dest"/pm-*.dump 2>/dev/null | head -1 || true)
[ -n "$dump" ] || fail "no pm-*.dump in $dest"
dump_name=$(basename "$dump")
age=$(( $(date +%s) - $(stat -c %Y "$dump") ))
[ "$age" -le $((2 * 86400)) ] || fail "newest dump is $((age / 3600)) h old (limit 48 h)"

password=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')
docker run -d --rm --name "$name" --network none -e POSTGRES_PASSWORD="$password" \
    postgres:16-alpine >/dev/null || fail "could not start the scratch container"

ready=0
for _ in $(seq 1 60); do
    # Initdb restarts the server once; require two consecutive successes on the final one.
    if docker exec "$name" pg_isready -U postgres -q 2>/dev/null \
        && docker exec "$name" psql -U postgres -qAt -c 'select 1' >/dev/null 2>&1; then
        ready=1; break
    fi
    sleep 2
done
[ "$ready" = 1 ] || fail "scratch postgres did not become ready"

docker exec "$name" createdb -U postgres pm_restore || fail "createdb failed"
docker exec -i "$name" pg_restore -U postgres -d pm_restore --no-owner --exit-on-error < "$dump" \
    >/dev/null 2>"${TMPDIR:-/tmp}/$name.err" \
    || { err=$(head -c 300 "${TMPDIR:-/tmp}/$name.err" | tr '\n' ' '); rm -f "${TMPDIR:-/tmp}/$name.err"; fail "pg_restore: $err"; }
rm -f "${TMPDIR:-/tmp}/$name.err"

q() { docker exec "$name" psql -U postgres -d pm_restore -qAt -v ON_ERROR_STOP=1 -c "$1"; }
rev=$(q "select count(*) from alembic_version") || fail "alembic_version unreadable"
[ "$rev" -gt 0 ] || fail "alembic_version is empty"
counts="alembic=$rev"
for t in account instrument ledger_transaction ledger_posting; do
    n=$(q "select count(*) from $t") || fail "cannot count $t"
    [ "$n" -gt 0 ] || fail "$t is empty"
    counts="$counts $t=$n"
done
echo "$(date -Is) restore-test ok $dump_name $counts"
