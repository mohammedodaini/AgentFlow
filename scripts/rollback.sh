#!/usr/bin/env bash
# Roll the running stack back to an image that already exists.
#
#     scripts/rollback.sh <version>
#
# **This does not touch the database, and that is the whole reason the migration
# rule in docs/operations.md exists.** Rolling the application back to yesterday's
# image leaves today's schema in place, so every migration has to be readable by
# the version before it. Additive changes are; a renamed column is not.
#
# If the bad deploy included a migration that is *not* backward compatible, this
# script is the wrong tool and the runbook says so — restore from the backup taken
# before the migration instead.
set -euo pipefail

cd "$(dirname "$0")/.."

VERSION="${1:-}"

if [[ -z "$VERSION" ]]; then
  echo "usage: scripts/rollback.sh <version>" >&2
  echo >&2
  echo "available:" >&2
  docker images "agentflow/api" --format '  {{.Tag}}  built {{.CreatedSince}}' >&2
  exit 1
fi

if ! docker image inspect "agentflow/api:${VERSION}" >/dev/null 2>&1; then
  echo "no image agentflow/api:${VERSION} on this host." >&2
  echo "Rollback re-runs an image that already exists; it does not build one." >&2
  exit 1
fi

echo "rolling back to ${VERSION}"
# No --build. The point of a rollback is to run something already verified, not
# to compile it again under pressure from a tree that may have moved on.
APP_VERSION="$VERSION" docker compose -f docker-compose.prod.yml up -d --no-build api worker web

echo "waiting for the API to report ready"
for _ in $(seq 1 30); do
  if curl -fsS "http://localhost:${API_PORT:-8000}/api/v1/health/ready" >/dev/null 2>&1; then
    echo "ready on ${VERSION}"
    exit 0
  fi
  sleep 2
done

echo "the API did not become ready within 60s — check 'make prod-logs'" >&2
exit 1
