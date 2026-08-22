#!/bin/sh
# Take a compressed dump on a schedule, prune old ones, and be loud about failure.
#
# A shell script mounted into the container rather than a one-liner in the
# compose file: this is the code that decides whether the data is recoverable,
# and it should be readable without counting YAML escapes.
#
# Run by the `backup` service in docker-compose.prod.yml.
set -eu

: "${POSTGRES_HOST:=postgres}"
: "${POSTGRES_USER:=agentflow}"
: "${POSTGRES_DB:=agentflow}"
: "${BACKUP_INTERVAL_SECONDS:=21600}"   # every six hours
: "${BACKUP_RETENTION_DAYS:=14}"
: "${BACKUP_DIR:=/backups}"

echo "backup: every ${BACKUP_INTERVAL_SECONDS}s, keeping ${BACKUP_RETENTION_DAYS} days in ${BACKUP_DIR}"

while true; do
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  target="${BACKUP_DIR}/agentflow-${stamp}.sql.gz"
  partial="${target}.partial"

  # Written to `.partial` and renamed only on success. A dump interrupted
  # halfway — the container stopped, the disk filled, the database restarted —
  # otherwise leaves a file that looks exactly like a backup and restores into a
  # half-populated database. An atomic rename means every file matching
  # `*.sql.gz` is a complete dump, which is what the restore script relies on.
  if pg_dump -h "$POSTGRES_HOST" -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$partial"; then
    mv "$partial" "$target"
    echo "backup: wrote $(basename "$target") ($(wc -c < "$target") bytes)"
  else
    rm -f "$partial"
    # Loud, and then keep going. A failed backup must not stop the *next* one:
    # the common causes — a restart, a brief network partition — are transient,
    # and a loop that exits on the first failure turns a five-minute blip into
    # "no backups since Tuesday".
    echo "backup: FAILED at ${stamp}" >&2
  fi

  # Pruned after a successful write, never before. Deleting first would, on a
  # run where the dump then fails, leave fewer backups than there were.
  find "$BACKUP_DIR" -name 'agentflow-*.sql.gz' -mtime "+${BACKUP_RETENTION_DAYS}" -delete
  # Any `.partial` older than a day is the corpse of a dump that never finished.
  find "$BACKUP_DIR" -name '*.partial' -mtime +1 -delete

  sleep "$BACKUP_INTERVAL_SECONDS"
done
