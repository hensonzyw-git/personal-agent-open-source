#!/usr/bin/env bash
# DEV-035: encrypt one consistent snapshot of both service databases plus the
# protected config into the offsite OSS bucket via restic, then prune.
#
# Runs as the personal-agent-backup user (created by install.sh), which is in
# both service groups so it can READ the staged snapshots -- never the live
# 0700 databases and never a secret. The snapshots themselves are produced by
# each service's own `*-db backup` subcommand, run by that service's own timer
# (personal-agent-db-backup.timer / personal-data-mcp-db-backup.timer) BEFORE
# this unit runs. This script fails closed if a staged snapshot is missing:
# shipping a backup that omits one database is worse than not shipping.
#
# Design 10.5: restic client-side encryption; bucket credentials and the
# repository key are SEPARATE (the repo key has an off-machine copy and is not
# in this env file). 30 daily / 12 weekly versions. `restic forget --prune`
# always carries an explicit policy -- `--prune` with no policy prints "no
# policy was specified" and exits 0 having deleted nothing, a false-pass trap
# recorded in the DEV-035 input-readiness evidence.
#
# Exit codes: non-zero marks the systemd unit failed, which is the alert
# channel (same as DEV-034's observe).

set -euo pipefail

STAGING=/var/backups/personal-agent
API_DB_SNAPSHOT="$STAGING/api/agent.latest.sqlite"
MCP_DB_SNAPSHOT="$STAGING/mcp/finance.latest.sqlite"
# The protected ledger config, staged by personal-data-mcp-db-backup. Read from
# staging, never from /var/lib/personal-data-mcp: that dir is 0700 and this user
# is deliberately not able to enter it.
STAGED_LEDGER_CONFIG="$STAGING/mcp/ledger.synthetic_test.2026.json"
UNIT_DIR=/etc/systemd/system
# The deletion-manifest export the restore must replay. Produced alongside the
# Agent snapshot by personal-agent-db-backup; see that unit.
DELETION_MANIFEST="$STAGING/api/deletion-manifest.json"

# Credentials: OSS AccessKey + restic repository + repository password. Root-
# owned, group-readable by the backup user only. Never logged.
ENV_FILE=/etc/personal-agent/restic.env
if [ ! -r "$ENV_FILE" ]; then
  echo "restic.env not readable at $ENV_FILE" >&2
  exit 2
fi
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY must be set in restic.env}"
: "${RESTIC_PASSWORD_FILE:?RESTIC_PASSWORD_FILE must be set in restic.env}"
: "${OSS_ACCESS_KEY_ID:?OSS_ACCESS_KEY_ID must be set in restic.env}"
: "${OSS_SECRET_ACCESS_KEY:?OSS_SECRET_ACCESS_KEY must be set in restic.env}"

export RESTIC_REPOSITORY RESTIC_PASSWORD_FILE
export AWS_ACCESS_KEY_ID="$OSS_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$OSS_SECRET_ACCESS_KEY"

echo "== DEV-035 backup $(date -u +%FT%TZ) =="

# The exact set restic will be handed. Declared once, checked once, passed once:
# a pre-flight over a different list than the transfer proves nothing about the
# transfer. On 2026-08-01 the checks covered three staged files while restic was
# handed eight paths, and the one that was never checked -- the ledger config,
# still sitting in the mcp 0700 live dir -- gave EPERM *after* restic had already
# written a partial snapshot to OSS.
INPUT_LABELS=(
  "agent snapshot"
  "finance snapshot"
  "deletion-manifest export"
  "ledger config"
  "api unit"
  "mcp unit"
  "mcp observe unit"
  "mcp observe timer"
)
INPUTS=(
  "$API_DB_SNAPSHOT"
  "$MCP_DB_SNAPSHOT"
  "$DELETION_MANIFEST"
  "$STAGED_LEDGER_CONFIG"
  "$UNIT_DIR/personal-agent-api.service"
  "$UNIT_DIR/personal-data-mcp.service"
  "$UNIT_DIR/personal-data-mcp-observe.service"
  "$UNIT_DIR/personal-data-mcp-observe.timer"
)

# Fail closed on a missing snapshot: a backup that ships one DB and not the
# other looks like success and hides the gap. The db-backup timers run before
# this one, but ordering alone is not the guarantee -- the file's presence is.
#
# Presence and readability are two different checks, and only the second one is
# what restic needs. `-s` is a stat, which succeeds on a 0600 file this user
# cannot open: on 2026-08-01 every `-s` check passed against staged files
# written under UMask=0077, and the first real read failed several lines later
# with the wrong error. Assert `-r` next to `-s`, before anything is uploaded.
for i in "${!INPUTS[@]}"; do
  path="${INPUTS[$i]}"
  label="${INPUT_LABELS[$i]}"
  if [ ! -s "$path" ]; then
    echo "FAIL: $label missing or empty at $path" >&2
    exit 1
  fi
  if [ ! -r "$path" ]; then
    echo "FAIL: $label at $path is not readable by $(id -un) -- inputs must be staged group-readable, never read from a service's 0700 live dir" >&2
    exit 1
  fi
done
# A valid manifest may legitimately hold zero entries (nothing has been deleted
# yet), but it must be a JSON array -- a non-array body is a broken export, and
# shipping one would let a future restore drill replay nothing and look
# successful. Reject anything that is not `[]` or a list of entries.
if ! /opt/personal-agent/.venv/bin/python -c \
  "import json; assert isinstance(json.load(open('$DELETION_MANIFEST')), list)"; then
  echo "FAIL: deletion-manifest export is not a JSON array at $DELETION_MANIFEST" >&2
  exit 1
fi

echo "== backing up =="
# restic exits 3 when it saved a snapshot but could not read every source. That
# is a partial snapshot in the repository, which is worse than no snapshot: a
# restore drill would find it and succeed against incomplete data. The
# pre-flight above should make it unreachable; treat it as fatal regardless.
restic backup \
  "${INPUTS[@]}" \
  --tag personal-agent \
  --tag "$(date -u +%Y-%m-%d)"

echo "== forgetting with explicit policy (30 daily / 12 weekly) =="
# Never call `restic forget --prune` without a --keep-* policy: it exits 0 and
# deletes nothing, which reads as success. The policy is the whole point.
restic forget \
  --keep-daily 30 \
  --keep-weekly 12 \
  --prune

echo "== restic check =="
restic check

echo "== DEV-035 backup OK =="
