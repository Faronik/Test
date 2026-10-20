#!/bin/sh
set -eu
cd "$(dirname "$0")"
command -v docker >/dev/null 2>&1 || { echo 'Install Docker Engine and Docker Compose first.'; exit 1; }
docker compose version >/dev/null
if [ ! -f .env ]; then
  umask 077
  password=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
  printf 'POSTGRES_PASSWORD=%s\nTOOLSTOCK_PORT=8000\nTOOLSTOCK_BIND=0.0.0.0\n' "$password" > .env
  unset password
fi
docker compose up -d --build
printf '\nToolstock is starting on port 8000. Check: docker compose ps\n'
