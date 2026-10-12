#!/usr/bin/env bash
# Set up this machine from an export made by ./docker/export.sh, or from the plain .sql backup that
# Admin -> Database Backup produces (for moving from the old non-Docker install).
#
#   ./docker/import.sh rdc-migration-XXXX.tar.gz
#   ./docker/import.sh --sql rdc_onboarding-XXXX.sql [--uploads path/to/app/static/uploads]
#
# Needs: Docker + Compose, and the project folder (git clone). Refuses to overwrite a database that
# already has data. For the --sql form, create .env.docker first (copy .env.docker.example, fill in).
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1

usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
SQL=""; UPLOADS=""; INSTANCE=""; ARCHIVE=""
TMP=$(mktemp -d "${TMPDIR:-/tmp}/rdc-import.XXXXXX" 2>/dev/null || mktemp -d)
trap 'rm -rf "$TMP"' EXIT

case "${1:-}" in
  --sql)
    [ -n "${2:-}" ] && [ -f "$2" ] || usage
    SQL=$2; shift 2
    if [ "${1:-}" = "--uploads" ]; then [ -d "${2:-}" ] || usage; UPLOADS=$2; fi ;;
  "" | -h | --help) usage ;;
  *)
    ARCHIVE=$1; [ -f "$ARCHIVE" ] || usage
    tar -xzf "$ARCHIVE" -C "$TMP"
    D=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n1)
    SQL=$D/db.sql; UPLOADS=$D/uploads; INSTANCE=$D/instance
    [ -f "$SQL" ] || { echo "That doesn't look like an export file (no db.sql inside)." >&2; exit 1; }
    if [ ! -f .env.docker ]; then cp "$D/env.docker" .env.docker; echo "Created .env.docker from the export."
    else echo "Keeping the existing .env.docker (the export's copy is NOT used)."; fi ;;
esac

[ -f .env.docker ] || { echo "Create .env.docker first (cp .env.docker.example .env.docker, then fill it in)." >&2; exit 1; }

wait_healthy() {   # $1 = service
  for i in $(seq 1 60); do
    id=$(docker compose ps -q "$1" 2>/dev/null || true)
    if [ -z "$id" ] && [ "$i" -ge 5 ]; then echo "The $1 container isn't running - see: docker compose ps -a / docker compose logs $1" >&2; exit 1; fi
    s=""; [ -n "$id" ] && s=$(docker inspect -f '{{.State.Health.Status}}' "$id" 2>/dev/null || true)
    [ "$s" = healthy ] && return 0; sleep 3
  done
  echo "$1 did not become healthy - see: docker compose logs $1" >&2; exit 1
}

echo "1/5 Starting the database..."
docker compose up -d db >/dev/null
wait_healthy db

EXISTING=$(docker compose exec -T db sh -c 'mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=\"$MYSQL_DATABASE\" AND table_name=\"users\""' 2>/dev/null | tr -d '\r')
if [ "${EXISTING:-0}" != "0" ]; then
  ROWS=$(docker compose exec -T db sh -c 'mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e "SELECT COUNT(*) FROM users"' 2>/dev/null | tr -d '\r')
  if [ "${ROWS:-0}" != "0" ]; then
    echo "The database here already has $ROWS user(s). Refusing to overwrite it." >&2
    echo "To start over on purpose:  docker compose down -v   (this DELETES the data), then run this again." >&2
    exit 1
  fi
fi

echo "2/5 Restoring the database..."
docker compose exec -T db sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" --default-character-set=utf8mb4' < "$SQL" 2>&1 | grep -v "Using a password" || true

echo "3/5 Starting the app..."
docker compose up -d --build app >/dev/null 2>&1
wait_healthy app

echo "4/5 Candidate documents..."
if [ -n "$UPLOADS" ] && [ -d "$UPLOADS" ]; then
  tar -C "$UPLOADS" -cf - . | docker compose exec -T app tar -C /app/app/static/uploads -xf -
  echo "    $(docker compose exec -T app sh -c 'ls /app/app/static/uploads | wc -l' | tr -d '\r') files in place"
else echo "    (none supplied - old requests' documents won't open until you copy them in)"; fi

echo "5/5 Truein cache..."
if [ -n "$INSTANCE" ] && [ -d "$INSTANCE" ] && [ -n "$(ls -A "$INSTANCE" 2>/dev/null)" ]; then
  tar -C "$INSTANCE" -cf - . | docker compose exec -T app tar -C /app/instance -xf -
  echo "    copied"
else echo "    (none - the app will pull it itself)"; fi

echo
echo "Done. Open http://localhost:5000 (or this machine's address), log in as before."
echo "Check Admin -> Email Settings -> App address points at THIS machine's address."
echo "Then switch the old install OFF - two live copies would both push hires to Truein."
