#!/usr/bin/env bash
# Build and tag a release from the current commit (M16 follow-up).
#
#     scripts/release.sh              # tag from HEAD
#     scripts/release.sh v1.2.0       # tag with a name as well
#
# The tag is the git SHA, because that is the only label that cannot lie about
# what is in the image. A hand-written version can be applied to the wrong
# commit; a SHA cannot. A human-readable name is added *alongside* it when one is
# given, never instead of it.
#
# Refuses to build from a dirty tree. An image tagged with a SHA whose working
# tree had uncommitted changes is an image nobody can reproduce — and it is the
# one you will be trying to reproduce, because it is the one that broke.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -n "$(git status --porcelain)" ]]; then
  echo "refusing to build: the working tree is dirty." >&2
  echo "An image tagged with this SHA would not match the commit." >&2
  git status --short >&2
  exit 1
fi

VERSION="$(git rev-parse --short HEAD)"
NAME="${1:-}"

echo "building agentflow ${VERSION}$([[ -n "$NAME" ]] && echo " (${NAME})")"

# `docker build` directly, NOT `docker compose build`, and the first attempt at
# this script got it wrong. Compose interpolates the entire file before doing
# anything, so building through it demanded SECRET_KEY, TOKEN_ENCRYPTION_KEY,
# POSTGRES_PASSWORD and the rest — the `:?` guards that make a *deploy* fail
# loudly also made a *build* impossible without them.
#
# That is backwards, and it matters beyond convenience: a build machine that
# needs the production signing key is a build machine that can be compromised for
# the production signing key. Building an image requires a source tree and
# nothing else, and this keeps it that way.
docker build -t "agentflow/api:${VERSION}" backend
docker build -t "agentflow/web:${VERSION}" frontend

if [[ -n "$NAME" ]]; then
  docker tag "agentflow/api:${VERSION}" "agentflow/api:${NAME}"
  docker tag "agentflow/web:${VERSION}" "agentflow/web:${NAME}"
fi

echo
echo "built:"
docker images "agentflow/*" --format '  {{.Repository}}:{{.Tag}}  {{.Size}}  {{.CreatedSince}}' \
  | grep -E ":${VERSION}|:${NAME:-__none__}" || true
echo
echo "deploy it with:  APP_VERSION=${VERSION} make prod-deploy"
echo "roll back with:  make prod-versions   # then APP_VERSION=<older> make prod-deploy"
