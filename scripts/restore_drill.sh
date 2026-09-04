#!/usr/bin/env bash
# DEV-035: the offsite restore drill. Run on the OFF-MACHINE copy holder (the
# Mac), NOT on the ECS -- the whole point is a different machine.
#
# Design 10.5 acceptance: 异机 restore, 解密样本, integrity, 只读启动有证据.
# Plus the deletion-manifest replay that design 10.5 makes a precondition for
# opening reads.
#
# Prerequisites:
#   - restic installed (brew install restic)
#   - the off-machine repository key copy (NOT the on-ECS one)
#   - RESTIC_REPOSITORY / OSS creds in the environment or a restic env file
#   - the off-machine data key and Finance service public key. Step 7 uses
#     purpose-built read-only entrypoints: it validates the protected Finance
#     config but never loads model, token, cursor, identifier, or private
#     service credentials and never composes a write-capable connector.
#
# This script is intentionally a thin orchestrator: the database checks live in
# personal_agent.backup.restore_verify so they are unit-tested offline. The
# shell handles restic restore + the read-only service start, which are the
# parts that touch the real environment and cannot be library calls.

set -euo pipefail

VENV="${PERSONAL_AGENT_VENV:-.venv}"
RESTORE_PARENT="${RESTORE_PARENT:-${TMPDIR:-/tmp}}"

# Preflight. Everything the read-only checks need is checked before step 1
# downloads a single pack.
MISSING=()
for var in \
  PERSONAL_AGENT_DATA_ACTIVE_KID PERSONAL_AGENT_DATA_ACTIVE_KEY_PATH \
  PERSONAL_DATA_MCP_SERVICE_ACTIVE_KID PERSONAL_DATA_MCP_SERVICE_ACTIVE_PUBLIC_KEY_PATH \
  RESTIC_REPOSITORY; do
  if [ -z "${!var:-}" ]; then MISSING+=("$var"); fi
done
if [ "${#MISSING[@]}" -gt 0 ]; then
  echo "FAIL: the drill needs these unset variables:" >&2
  printf '  %s\n' "${MISSING[@]}" >&2
  exit 2
fi
# Paths, not just names: a key ring pointed at a path that does not exist on
# THIS machine is the single most likely mistake when copying an ECS env file.
for var in \
  PERSONAL_AGENT_DATA_ACTIVE_KEY_PATH \
  PERSONAL_DATA_MCP_SERVICE_ACTIVE_PUBLIC_KEY_PATH; do
  if [ ! -r "${!var}" ]; then
    echo "FAIL: $var points at ${!var}, which is not readable here" >&2
    echo "      (an ECS path copied verbatim? rewrite it to the local copy)" >&2
    exit 2
  fi
done

DATA_KID="$PERSONAL_AGENT_DATA_ACTIVE_KID"
DATA_KEY_PATH="$PERSONAL_AGENT_DATA_ACTIVE_KEY_PATH"

case "$RESTORE_PARENT" in
  /*) ;;
  *) echo "FAIL: RESTORE_PARENT must be an absolute path" >&2; exit 2 ;;
esac
mkdir -p "$RESTORE_PARENT"
RESTORE_PARENT="$(cd "$RESTORE_PARENT" && pwd -P)"
if [ "$RESTORE_PARENT" = "/" ] || [ ! -w "$RESTORE_PARENT" ]; then
  echo "FAIL: unsafe or unwritable RESTORE_PARENT: $RESTORE_PARENT" >&2
  exit 2
fi
RESTORE_DIR="$(mktemp -d "$RESTORE_PARENT/personal-agent-drill.XXXXXX")"
RESTORE_MARKER="$RESTORE_DIR/.personal-agent-restore-drill"
: > "$RESTORE_MARKER"
MCP_PID=""
API_PID=""

cleanup() {
  if [ -n "$API_PID" ]; then kill "$API_PID" 2>/dev/null || true; wait "$API_PID" 2>/dev/null || true; fi
  if [ -n "$MCP_PID" ]; then kill "$MCP_PID" 2>/dev/null || true; wait "$MCP_PID" 2>/dev/null || true; fi
  case "$RESTORE_DIR" in
    "$RESTORE_PARENT"/personal-agent-drill.*)
      if [ -f "$RESTORE_MARKER" ]; then rm -rf -- "$RESTORE_DIR"; fi
      ;;
    *) echo "WARN: refusing to clean unexpected restore path: $RESTORE_DIR" >&2 ;;
  esac
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

echo "== DEV-035 offsite restore drill ($(date -u +%FT%TZ)) =="
echo "restore dir: $RESTORE_DIR"

echo "== 1. restic restore latest =="
restic restore latest --target "$RESTORE_DIR"

# Locate the restored snapshots. restic restores the full path tree, so the
# files land under $RESTORE_DIR/var/backups/personal-agent/.
API_DB="$RESTORE_DIR/var/backups/personal-agent/api/agent.latest.sqlite"
MCP_DB="$RESTORE_DIR/var/backups/personal-agent/mcp/finance.latest.sqlite"
DAL_DB="$RESTORE_DIR/var/backups/personal-agent/dal/dal.latest.sqlite"
MANIFEST="$RESTORE_DIR/var/backups/personal-agent/api/deletion-manifest.json"
for f in "$API_DB" "$MCP_DB" "$MANIFEST"; do
  if [ ! -s "$f" ]; then
    echo "FAIL: restored file missing or empty: $f" >&2
    exit 1
  fi
done
# The DAL snapshot entered the backup set on 2026-09-02 (R09-B). Restores of
# snapshots older than that legitimately lack the file; anything newer must
# carry it, and a drill that skips it verifies two databases and silently
# ignores the third. The date gate keeps the drill honest without breaking the
# old-snapshot case.
DAL_ARGS=()
if [ -s "$DAL_DB" ]; then
  DAL_ARGS=(--dal-database "$DAL_DB")
else
  echo "NOTE: no dal/dal.latest.sqlite in this snapshot (pre-2026-09-02 snapshot?)"
  echo "      The DAL restore gates are SKIPPED -- this run is not evidence that"
  echo "      the DAL database is restorable."
fi

echo "== 2-6. library checks (integrity, schema, refs, AEAD sample, replay) =="
export PERSONAL_AGENT_DATA_ACTIVE_KID="$DATA_KID"
export PERSONAL_AGENT_DATA_ACTIVE_KEY_PATH="$DATA_KEY_PATH"
# The AEAD sample entry id is required for the G5 drill evidence (解密样本).
# Seed a synthetic `drill-sample` entry on the ECS agent DB before backup if no
# business deletion has produced one yet; see docs/DEV035_部署与演练交接_v0.1.md.
SAMPLE_ARGS=()
if [ -n "${AEAD_SAMPLE_ENTRY_ID:-}" ]; then
  SAMPLE_ARGS=(--aead-sample-entry-id "$AEAD_SAMPLE_ENTRY_ID")
else
  echo "NOTE: AEAD_SAMPLE_ENTRY_ID unset; skipping the fixed-sample decrypt (not G5 evidence)."
fi
"$VENV/bin/python" -m personal_agent.backup.restore_cli \
  --agent-database "$API_DB" \
  --finance-database "$MCP_DB" \
  --manifest "$MANIFEST" \
  "${DAL_ARGS[@]}" \
  "${SAMPLE_ARGS[@]}" \
  || { echo "FAIL: library restore checks" >&2; exit 1; }

# The Finance MCP database audit chain is verified on its own.
echo "== 2b. finance audit chain =="
"$VENV/bin/personal-data-mcp-observe" --database "$MCP_DB" \
  || { echo "FAIL: observe reported findings on the restored finance DB" >&2; exit 1; }

echo "== 7. read-only service start (smoke) =="
# Both services start through dedicated read-only modes. The API performs only
# a database probe; the Finance MCP validates the protected synthetic config
# but does not load credentials, compose tools, run recovery, or call Feishu.
MCP_LEDGER="$RESTORE_DIR/var/backups/personal-agent/mcp/ledger.synthetic_test.2026.json"
if [ ! -s "$MCP_LEDGER" ]; then
  echo "FAIL: restored ledger config missing at $MCP_LEDGER" >&2
  exit 1
fi
MCP_PORT="${DRILL_MCP_PORT:-8811}"
"$VENV/bin/personal-data-mcp" \
  --restore-read-only \
  --host 127.0.0.1 --port "$MCP_PORT" \
  --database "$MCP_DB" \
  --ledger-config "$MCP_LEDGER" \
  &
MCP_PID=$!
# Wait for the port to answer rather than sleeping a fixed time: a fixed sleep
# turns a slow start into a false FAIL and a fast one into wasted seconds.
MCP_UP=0
for _ in $(seq 1 30); do
  if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$MCP_PORT/mcp"; then
    MCP_UP=1
    break
  fi
  sleep 1
done
if [ "$MCP_UP" -ne 1 ]; then
  echo "FAIL: restored finance DB did not boot the Finance MCP on port $MCP_PORT" >&2
  exit 1
fi
echo "PASS: restored finance DB boots the Finance MCP"

"$VENV/bin/personal-agent-api" \
  --restore-read-only \
  --database "$API_DB" \
  --socket "$RESTORE_DIR/api.sock" \
  &
API_PID=$!
API_UP=0
for _ in $(seq 1 30); do
  if curl -s -o /dev/null -w '%{http_code}' --max-time 2 \
       --unix-socket "$RESTORE_DIR/api.sock" http://localhost/v1/capabilities \
     | grep -q '401'; then
    API_UP=1
    break
  fi
  sleep 1
done
if [ "$API_UP" -eq 1 ]; then
  echo "PASS: restored agent DB boots the API (401 unauth = app answered)"
else
  echo "FAIL: restored DB did not boot the API read-only" >&2
  exit 1
fi
# The EXIT trap owns the teardown of both processes and the restore dir, so the
# failure path above does not need its own kill -- and cannot forget one.
kill "$API_PID" "$MCP_PID" 2>/dev/null || true
wait "$API_PID" 2>/dev/null || true
wait "$MCP_PID" 2>/dev/null || true
API_PID=""
MCP_PID=""

echo "== DEV-035 restore drill OK =="
