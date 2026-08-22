#!/usr/bin/env bash
# Restore the database from a dump. **Destructive, and it says so.**
#
#     scripts/restore.sh                       # newest backup
#     scripts/restore.sh backups/agentflow-....sql.gz
#
# This is the recovery path for the case `scripts/rollback.sh` cannot cover: a
# release whose migration was not backward compatible, or data lost to a bug.
# It replaces the current database with the contents of the dump, so everything
# written since that dump is gone. See docs/operations.md.
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.prod.yml"
BACKUP="${1:-}"

# **There is no default, and a drill is what taught that.**
#
# The first version of this script restored the newest backup when given no
# argument. That is the wrong default in the exact situation the script exists
# for: backups run on a schedule, so by the time somebody notices the problem the
# scheduler has already taken one or more backups *of the problem*. The rehearsal
# deleted every user, ran `restore.sh` with no argument, and it faithfully
# restored the empty database — reporting success.
#
# So the operator names the file. The listing below is there to make that easy,
# not to make it optional: the question "when did this start?" has to be answered
# before a restore, and no default can answer it.
if [[ -z "$BACKUP" ]]; then
  echo "Name the backup to restore from. There is no default — see below." >&2
  echo >&2
  echo "The newest backup is very likely a backup of whatever went wrong:" >&2
  echo "backups run on a schedule and do not know an incident has started." >&2
  echo >&2
  echo "Available (newest first):" >&2
  ls -1t backups/agentflow-*.sql.gz 2>/dev/null | head -20 | while read -r f; do
    printf '  %s  %s\n' "$f" "$(date -r "$f" '+%Y-%m-%d %H:%M' 2>/dev/null || stat -f %Sm "$f")" >&2
  done
  echo >&2
  echo "  scripts/restore.sh backups/agentflow-<stamp>.sql.gz" >&2
  exit 1
fi

if [[ ! -f "$BACKUP" ]]; then
  echo "no such backup: $BACKUP" >&2
  exit 1
fi

echo "About to REPLACE the database with:"
echo "  $BACKUP  ($(wc -c < "$BACKUP") bytes, $(date -r "$BACKUP" 2>/dev/null || stat -f %Sm "$BACKUP"))"
echo
echo "Everything written since then will be lost."

if [[ "${RESTORE_YES:-}" != "1" ]]; then
  read -r -p "Type the word restore to continue: " answer
  [[ "$answer" == "restore" ]] || { echo "aborted."; exit 1; }
fi

# The application is stopped first. Restoring underneath a running API means
# open transactions against tables being dropped, and a worker writing rows into
# a schema that is halfway replaced — a database that is neither the old state
# nor the new one.
echo "stopping the application"
$COMPOSE stop api worker web >/dev/null

echo "restoring"
# `--clean --if-exists` is in the dump's own header via pg_dump defaults? It is
# not — plain `pg_dump` emits CREATE without DROP. So the schema is dropped here
# instead, which is also what makes this idempotent: restoring twice gives the
# same result rather than failing on objects that already exist.
gunzip -c "$BACKUP" | $COMPOSE exec -T postgres psql -v ON_ERROR_STOP=1 \
  -U "${POSTGRES_USER:-agentflow}" -d "${POSTGRES_DB:-agentflow}" \
  -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" -f - >/dev/null

echo "starting the application"
$COMPOSE start api worker web >/dev/null

for _ in $(seq 1 30); do
  if curl -fsS "http://localhost:${API_PORT:-8000}/api/v1/health/ready" >/dev/null 2>&1; then
    echo "restored from $(basename "$BACKUP") and ready"
    exit 0
  fi
  sleep 2
done

echo "restored, but the API did not become ready within 60s — check 'make prod-logs'" >&2
exit 1
