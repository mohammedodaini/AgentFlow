#!/usr/bin/env bash
#
# Start one unattended Claude run against AgentFlow.
#
# Invoked by launchd (scripts/com.agentflow.continue.plist) or by hand. It does
# the things a headless session cannot do for itself — make sure the datastores
# are up, refuse to start on a dirty tree — and then hands over to Claude with
# docs/CONTINUE.md as the brief.
#
# Everything risky is bounded here rather than trusted to the model: a dollar
# budget, a kill switch, a refusal to run twice at once, and a log per run.

set -euo pipefail

REPO="${AGENTFLOW_REPO:-$HOME/AgentFlow}"
BUDGET_USD="${AGENTFLOW_BUDGET_USD:-40}"
MODEL="${AGENTFLOW_MODEL:-opus}"
PERMISSION_MODE="${AGENTFLOW_PERMISSION_MODE:-acceptEdits}"

CLAUDE_BIN="${AGENTFLOW_CLAUDE_BIN:-$HOME/.local/bin/claude}"
COLIMA_BIN="${AGENTFLOW_COLIMA_BIN:-/opt/homebrew/bin/colima}"
DOCKER_BIN="${AGENTFLOW_DOCKER_BIN:-/opt/homebrew/bin/docker}"

LOG_DIR="$REPO/backend/var/logs"
STOP_FILE="$REPO/backend/var/STOP"
LOCK_FILE="$REPO/backend/var/continue.lock"
LOG_FILE="$LOG_DIR/continue-$(date +%Y%m%d-%H%M%S).log"

mkdir -p "$LOG_DIR"
exec >>"$LOG_FILE" 2>&1

say() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

say "=== continue-agentflow starting ==="

# ---------------------------------------------------------------------------
# Refusals. Each one is cheaper than the situation it prevents.
# ---------------------------------------------------------------------------

# The kill switch. `touch backend/var/STOP` stops every future run without
# unloading launchd or editing anything — the thing you want to be able to do
# from a phone.
if [[ -f "$STOP_FILE" ]]; then
  say "STOP file present at $STOP_FILE — refusing to run. Delete it to resume."
  exit 0
fi

if [[ ! -x "$CLAUDE_BIN" ]]; then
  say "FATAL: claude CLI not found at $CLAUDE_BIN"
  exit 1
fi

cd "$REPO"

# Two runs sharing one working tree would interleave edits into an
# uninterpretable mess. `flock` is not on macOS by default, so this is a
# noclobber lock: atomic, and it self-heals if a previous run was killed.
if ! (set -o noclobber; echo "$$" >"$LOCK_FILE") 2>/dev/null; then
  OWNER=$(cat "$LOCK_FILE" 2>/dev/null || echo "unknown")
  if kill -0 "$OWNER" 2>/dev/null; then
    say "another run (pid $OWNER) is active — exiting"
    exit 0
  fi
  say "stale lock from pid $OWNER — reclaiming"
  echo "$$" >"$LOCK_FILE"
fi
trap 'rm -f "$LOCK_FILE"' EXIT

# A dirty tree means somebody was mid-thought. Committing on top of that, or
# worse resetting it, is not a decision an unattended process gets to make.
if [[ -n "$(git status --porcelain)" ]]; then
  say "working tree is dirty — refusing to run. Commit or stash first:"
  git status --short
  exit 0
fi

say "on branch $(git rev-parse --abbrev-ref HEAD) at $(git rev-parse --short HEAD)"

# ---------------------------------------------------------------------------
# Datastores. The VM does not auto-start on boot, so a scheduled run has to.
# ---------------------------------------------------------------------------

if ! "$DOCKER_BIN" info >/dev/null 2>&1; then
  say "docker unavailable — starting Colima"
  "$COLIMA_BIN" start || { say "FATAL: colima start failed"; exit 1; }
fi

say "bringing up Postgres and Redis"
"$DOCKER_BIN" compose up -d >/dev/null || { say "FATAL: compose up failed"; exit 1; }

# Compose returns as soon as the containers exist, which is well before
# Postgres accepts connections. Without this wait the first run of the test
# suite fails with connection errors that look like a code problem.
for attempt in $(seq 1 30); do
  if "$DOCKER_BIN" exec agentflow-postgres-1 pg_isready -U agentflow >/dev/null 2>&1; then
    say "postgres ready after ${attempt}s"
    break
  fi
  if [[ "$attempt" -eq 30 ]]; then
    say "FATAL: postgres not ready after 30s"
    exit 1
  fi
  sleep 1
done

# ---------------------------------------------------------------------------
# Hand over.
# ---------------------------------------------------------------------------

say "starting Claude (model=$MODEL, budget=\$$BUDGET_USD, mode=$PERMISSION_MODE)"

# The prompt is deliberately short. Anything worth saying belongs in
# docs/CONTINUE.md, where it is version-controlled, reviewable, and editable
# without touching this script.
set +e
"$CLAUDE_BIN" -p "Read docs/CONTINUE.md and follow it exactly. Build the next \
unfinished milestone to completion, then stop. Do not push." \
  --model "$MODEL" \
  --permission-mode "$PERMISSION_MODE" \
  --max-budget-usd "$BUDGET_USD" \
  --output-format text
STATUS=$?
set -e

say "claude exited with status $STATUS"
say "tree is now: $(git rev-parse --short HEAD) $(git status --porcelain | wc -l | tr -d ' ') uncommitted file(s)"
say "=== continue-agentflow finished ==="
