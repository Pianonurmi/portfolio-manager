#!/usr/bin/env bash
# Nightly app-consistent dump (NFR-10). The VM-level backup picks up the dump directory.
set -euo pipefail
cd "$(dirname "$0")"
dest="${PM_BACKUP_DIR:-$HOME/backups/portfolio-manager}"
mkdir -p "$dest"
f="$dest/pm-$(date +%Y%m%d-%H%M).dump"
docker compose exec -T db pg_dump -U pm -Fc pm > "$f.tmp"
mv "$f.tmp" "$f"
find "$dest" -name 'pm-*.dump' -mtime +30 -delete
echo "$(date -Is) backup ok $(du -h "$f" | cut -f1) $f"
