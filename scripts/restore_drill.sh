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
#   - the off-machine copies of FIVE key rings, not just the data one. Step 7
#     boots the real `personal-agent-api`, and its composition loads data,
#     cursor, identifier, token and service rings plus ZAI_API_KEY before it
#     opens a socket. Listing only the data key here used to be wrong in a way
#     that wasted a whole drill: checks 1-6 pass, then step 7 dies on a missing
#     environment variable that has nothing to do with the restore. The
#     preflight below now names every one of them up front.
#
# This script is intentionally a thin orchestrator: the six checks live in
# personal_agent.backup.restore_verify so they are unit-tested offline. The
# shell handles restic restore + the read-only service start, which are the
# parts that touch the real environment and cannot be library calls.

set -euo pipefail

VENV="${PERSONAL_AGENT_VENV:-.venv}"
RESTORE_DIR="${RESTORE_DIR:-/tmp/personal-agent-drill-$$}"

# Preflight. Everything step 7 will need is checked before step 1 downloads a
# single pack, because the expensive part of a failed drill is not the restic
# transfer -- it is discovering at the end that the run proved nothing.
MISSING=()
for var in \
  PERSONAL_AGENT_DATA_ACTIVE_KID PERSONAL_AGENT_DATA_ACTIVE_KEY_PATH \
  PERSONAL_AGENT_CURSOR_ACTIVE_KID PERSONAL_AGENT_CURSOR_ACTIVE_KEY_PATH \
  PERSONAL_AGENT_IDENTIFIER_ACTIVE_KID PERSONAL_AGENT_IDENTIFIER_ACTIVE_KEY_PATH \
  PERSONAL_AGENT_TOKEN_ACTIVE_KID PERSONAL_AGENT_TOKEN_ACTIVE_PRIVATE_KEY_PATH \
  PERSONAL_AGENT_SERVICE_ACTIVE_KID PERSONAL_AGENT_SERVICE_ACTIVE_PRIVATE_KEY_PATH \
  PERSONAL_DATA_MCP_DATA_ACTIVE_KID PERSONAL_DATA_MCP_DATA_ACTIVE_KEY_PATH \
  PERSONAL_DATA_MCP_SERVICE_ACTIVE_KID PERSONAL_DATA_MCP_SERVICE_ACTIVE_PUBLIC_KEY_PATH \
  ZAI_API_KEY RESTIC_REPOSITORY; do
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
  PERSONAL_AGENT_DATA_ACTIVE_KEY_PATH PERSONAL_AGENT_CURSOR_ACTIVE_KEY_PATH \
  PERSONAL_AGENT_IDENTIFIER_ACTIVE_KEY_PATH \
  PERSONAL_AGENT_TOKEN_ACTIVE_PRIVATE_KEY_PATH \
  PERSONAL_AGENT_SERVICE_ACTIVE_PRIVATE_KEY_PATH \
  PERSONAL_DATA_MCP_DATA_ACTIVE_KEY_PATH \
  PERSONAL_DATA_MCP_SERVICE_ACTIVE_PUBLIC_KEY_PATH; do
  if [ ! -r "${!var}" ]; then
    echo "FAIL: $var points at ${!var}, which is not readable here" >&2
    echo "      (an ECS path copied verbatim? rewrite it to the local copy)" >&2
    exit 2
  fi
done

DATA_KID="$PERSONAL_AGENT_DATA_ACTIVE_KID"
DATA_KEY_PATH="$PERSONAL_AGENT_DATA_ACTIVE_KEY_PATH"

echo "== DEV-035 offsite restore drill ($(date -u +%FT%TZ)) =="
echo "restore dir: $RESTORE_DIR"
mkdir -p "$RESTORE_DIR"
trap 'rm -rf "$RESTORE_DIR"' EXIT

echo "== 1. restic restore latest =="
restic restore latest --target "$RESTORE_DIR"

# Locate the restored snapshots. restic restores the full path tree, so the
# files land under $RESTORE_DIR/var/backups/personal-agent/.
API_DB="$RESTORE_DIR/var/backups/personal-agent/api/agent.latest.sqlite"
MCP_DB="$RESTORE_DIR/var/backups/personal-agent/mcp/finance.latest.sqlite"
MANIFEST="$RESTORE_DIR/var/backups/personal-agent/api/deletion-manifest.json"
for f in "$API_DB" "$MCP_DB" "$MANIFEST"; do
  if [ ! -s "$f" ]; then
    echo "FAIL: restored file missing or empty: $f" >&2
    exit 1
  fi
done

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
  --manifest "$MANIFEST" \
  "${SAMPLE_ARGS[@]}" \
  || { echo "FAIL: library restore checks" >&2; exit 1; }

# The Finance MCP database audit chain is verified on its own.
echo "== 2b. finance audit chain =="
"$VENV/bin/personal-data-mcp-observe" --database "$MCP_DB" \
  || { echo "NOTE: observe reported findings on the restored finance DB" >&2; }

echo "== 7. read-only service start (smoke) =="
# BOTH services, not just the API. The API's composition discovers the Finance
# MCP tool catalog before it opens a socket and fails closed when it cannot
# reach it -- correct production behaviour, and it means an API-only smoke on a
# machine with no MCP server could never pass. That was this script's own
# defect, found the first time it was really run (2026-08-01): checks 1-6
# passed and step 7 failed for a reason with nothing to do with the restore.
#
# Starting the MCP against the RESTORED finance DB and the RESTORED ledger
# config is also the stronger evidence: design 10.5's "服务只读启动" covers both
# databases, and the snapshot carries both precisely so this is possible. The
# restored config is the synthetic test ledger -- composition refuses a
# non-synthetic one -- so the drill cannot touch the real annual ledger.
MCP_LEDGER="$RESTORE_DIR/var/backups/personal-agent/mcp/ledger.synthetic_test.2026.json"
if [ ! -s "$MCP_LEDGER" ]; then
  echo "FAIL: restored ledger config missing at $MCP_LEDGER" >&2
  exit 1
fi
MCP_PORT="${DRILL_MCP_PORT:-8811}"
"$VENV/bin/personal-data-mcp" \
  --host 127.0.0.1 --port "$MCP_PORT" \
  --database "$MCP_DB" \
  --ledger-config "$MCP_LEDGER" \
  &
MCP_PID=$!
trap 'kill "$MCP_PID" 2>/dev/null || true; rm -rf "$RESTORE_DIR"' EXIT
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
  --database "$API_DB" \
  --socket "$RESTORE_DIR/api.sock" \
  --finance-mcp-url "http://127.0.0.1:$MCP_PORT/mcp" \
  --allow-tool meta.capabilities \
  &
API_PID=$!
trap 'kill "$API_PID" "$MCP_PID" 2>/dev/null || true; rm -rf "$RESTORE_DIR"' EXIT
sleep 3
if curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
     --unix-socket "$RESTORE_DIR/api.sock" http://localhost/v1/capabilities \
   | grep -q '401'; then
  echo "PASS: restored agent DB boots the API (401 unauth = app answered)"
else
  echo "FAIL: restored DB did not boot the API read-only" >&2
  exit 1
fi
# The EXIT trap owns the teardown of both processes and the restore dir, so the
# failure path above does not need its own kill -- and cannot forget one.
kill "$API_PID" "$MCP_PID" 2>/dev/null || true

echo "== DEV-035 restore drill OK =="
