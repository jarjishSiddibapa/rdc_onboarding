#!/usr/bin/env bash
# Pack EVERYTHING that makes this install yours into one file, for moving to another machine:
#   the database, candidate documents/profile pictures, the Truein cache and .env.docker.
# Run from anywhere, on the machine where the Docker stack is running:   ./docker/export.sh
# Result: rdc-migration-<date>.tar.gz in the project folder. It holds personal data and passwords,
# so copy it over a trusted channel (scp / encrypted USB) and delete it afterwards.
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1            # stop Git Bash on Windows rewriting /app/... paths

[ -f .env.docker ] || { echo "No .env.docker here - run this from the project folder of the Docker install." >&2; exit 1; }
docker compose ps --status running --services | grep -qx db  || { echo "The db container isn't running (docker compose up -d)." >&2; exit 1; }
docker compose ps --status running --services | grep -qx app || { echo "The app container isn't running (docker compose up -d)." >&2; exit 1; }

STAMP=$(date +%Y%m%d-%H%M%S)
NAME="rdc-migration-$STAMP"
rm -rf "$NAME"; mkdir -p "$NAME/uploads" "$NAME/instance"
trap 'rm -rf "$NAME"' EXIT

echo "1/4 Database..."
docker compose exec -T db sh -c 'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines --triggers --default-character-set=utf8mb4 --databases "$MYSQL_DATABASE"' 2>/dev/null > "$NAME/db.sql"
tail -n 3 "$NAME/db.sql" | grep -q "Dump completed" || { echo "Database dump looks incomplete - aborting." >&2; exit 1; }

echo "2/4 Candidate documents..."
docker compose exec -T app tar -C /app/app/static/uploads -cf - . | tar -xf - -C "$NAME/uploads"

echo "3/4 Truein cache..."
docker compose exec -T app tar -C /app/instance -cf - . | tar -xf - -C "$NAME/instance"

echo "4/4 Settings..."
cp .env.docker "$NAME/env.docker"

tar -czf "$NAME.tar.gz" "$NAME"
chmod 600 "$NAME.tar.gz" 2>/dev/null || true
echo
echo "Done: $NAME.tar.gz  ($(du -h "$NAME.tar.gz" | cut -f1))"
echo "On the new machine: get the project (git clone), put this file in its folder, then:  ./docker/import.sh $NAME.tar.gz"
