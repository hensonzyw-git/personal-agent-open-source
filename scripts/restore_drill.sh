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
#   - the data key ring off-machine copy (PERSONAL_AGENT_DATA_ACTIVE_*)
#   - RESTIC_REPOSITORY / OSS creds in the environment or a restic env file
#
# This script is intentionally a thin orchestrator: the six checks live in
# personal_agent.backup.restore_verify so they are unit-tested offline. The
# shell handles restic restore + the read-only service start, which are the
# parts that touch the real environment and cannot be library calls.

set -euo pipefail

VENV="${PERSONAL_AGENT_VENV:-.venv}"
RESTORE_DIR="${RESTORE_DIR:-/tmp/personal-agent-drill-$$}"
DATA_KID="${PERSONAL_AGENT_DATA_ACTIVE_KID:?set PERSONAL_AGENT_DATA_ACTIVE_KID}"
DATA_KEY_PATH="${PERSONAL_AGENT_DATA_ACTIVE_KEY_PATH:?set PERSONAL_AGENT_DATA_ACTIVE_KEY_PATH}"

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
# Start the Agent API against the restored DB with an empty allow-tool set, so
# no write tool is exposed. This only proves the restored DB boots the service
# read-only; it is not left running.
"$VENV/bin/personal-agent-api" \
  --database "$API_DB" \
  --socket "$RESTORE_DIR/api.sock" \
  --finance-mcp-url http://127.0.0.1:8811/mcp \
  --allow-tool meta.capabilities \
  &
API_PID=$!
sleep 3
if curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
     --unix-socket "$RESTORE_DIR/api.sock" http://localhost/v1/capabilities \
   | grep -q '401'; then
  echo "PASS: restored DB boots the API (401 unauth = app answered)"
else
  echo "FAIL: restored DB did not boot the API read-only" >&2
  kill "$API_PID" 2>/dev/null || true
  exit 1
fi
kill "$API_PID" 2>/dev/null || true

echo "== DEV-035 restore drill OK =="
