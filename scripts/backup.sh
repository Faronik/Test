#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
backup_dir="${1:-.backups/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$backup_dir"
umask 077
app_running=$(docker compose ps --status running -q app)
restart_app() { if [ -n "$app_running" ]; then docker compose start app >/dev/null; fi; }
trap restart_app EXIT HUP INT TERM
if [ -n "$app_running" ]; then docker compose stop app; fi
docker compose exec -T db pg_dump -U toolstock -d toolstock -Fc > "$backup_dir/database.dump"
docker compose run --rm --no-deps --entrypoint tar app -C /var/lib/toolstock -cf - photos > "$backup_dir/photos.tar"
printf 'Backup complete: %s\n' "$backup_dir"
