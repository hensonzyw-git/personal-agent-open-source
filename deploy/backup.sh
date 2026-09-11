#!/usr/bin/env bash
# DEV-035: encrypt one consistent snapshot of both service databases plus the
# protected config into the offsite OSS bucket via restic, then prune.
#
# Runs as the personal-agent-backup user (created by install.sh), which belongs
# to neither service group. It reads snapshots through setgid staging
# directories owned by its own group, never the live 0700 databases or service
# secrets. The snapshots themselves are produced by each service's own `*-db
# backup` subcommand, run by that service's own timer
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
BACKUP_STATE_DIR=/var/lib/personal-agent-backup
MARKER="$BACKUP_STATE_DIR/last-successful-backup"
MCP_DB_SNAPSHOT="$STAGING/mcp/finance.latest.sqlite"
RISK_DB_SNAPSHOT="$STAGING/api/risk_monitor.latest.sqlite"
# The protected ledger config, staged by personal-data-mcp-db-backup. Read from
# staging, never from /var/lib/personal-data-mcp: that dir is 0700 and this user
# is deliberately not able to enter it.
STAGED_LEDGER_CONFIG="$STAGING/mcp/ledger.synthetic_test.2026.json"
UNIT_DIR=/etc/systemd/system
# The API service publishes an immutable DB + deletion-manifest + ciphertext
# media run here. This backup user holds this separate bundle lock shared from
# verification through restic, so the producer/GC cannot switch or reclaim the
# run under a consumer that has already checked it.
BUNDLE_STAGE="$STAGING/api"
BUNDLE_LOCK="$BUNDLE_STAGE/media-bundle.lock"

# DEV-036 idempotency is one successful offsite snapshot per Shanghai calendar
# day. systemd serialises starts of this unit, while flock also covers an
# operator invoking the script directly during a timer run. A concurrent caller
# waits, then re-checks the marker: it skips after success or performs the retry
# after failure. A second trigger after today's success exits before restic.
if [ ! -d "$BACKUP_STATE_DIR" ]; then
  echo "backup state directory missing at $BACKUP_STATE_DIR (run install.sh)" >&2
  exit 2
fi
exec 9>"$BACKUP_STATE_DIR/backup.lock"
flock 9
if [ -f "$MARKER" ]; then
  last_success=$(tr -d '\r\n' < "$MARKER")
  if ! last_success_day=$(TZ=Asia/Shanghai date -d "$last_success" +%F 2>/dev/null); then
    echo "invalid backup-success marker at $MARKER" >&2
    exit 2
  fi
  today=$(TZ=Asia/Shanghai date +%F)
  if [ "$last_success_day" = "$today" ]; then
    echo "verified backup already succeeded on $today; duplicate trigger is a no-op"
    exit 0
  fi
fi

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

if [ ! -r "$BUNDLE_LOCK" ]; then
  echo "FAIL: media bundle lock is not readable at $BUNDLE_LOCK" >&2
  exit 1
fi
exec 8<"$BUNDLE_LOCK"
flock -s 8
BUNDLE_INFO=$(/opt/personal-agent/.venv/bin/personal-agent-media-backup-bundle \
  verify --stage-root "$BUNDLE_STAGE")
BUNDLE_DIR=$(printf '%s' "$BUNDLE_INFO" | /opt/personal-agent/.venv/bin/python -c \
  'import json, sys; print(json.load(sys.stdin)["path"])')
case "$BUNDLE_DIR" in
  "$BUNDLE_STAGE"/media-runs/*) ;;
  *) echo "FAIL: verified media bundle path escapes staging: $BUNDLE_DIR" >&2; exit 1 ;;
esac
API_DB_SNAPSHOT="$BUNDLE_DIR/agent.sqlite"
DELETION_MANIFEST="$BUNDLE_DIR/deletion-manifest.json"

# The exact set restic will be handed. Declared once, checked once, passed once:
# a pre-flight over a different list than the transfer proves nothing about the
# transfer. On 2026-08-01 the checks covered three staged files while restic was
# handed eight paths, and the one that was never checked -- the ledger config,
# still sitting in the mcp 0700 live dir -- gave EPERM *after* restic had already
# written a partial snapshot to OSS.
INPUT_LABELS=(
  "complete media bundle"
  "finance snapshot"
  "risk snapshot"
  "ledger config"
  "api unit"
  "mcp unit"
  "mcp observe unit"
  "mcp observe timer"
)
INPUTS=(
  "$BUNDLE_DIR"
  "$MCP_DB_SNAPSHOT"
  "$RISK_DB_SNAPSHOT"
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
  if [ -d "$path" ]; then
    if [ ! -r "$path" ]; then
      echo "FAIL: $label directory is not readable at $path" >&2
      exit 1
    fi
    continue
  fi
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
# yet), but it must be the sealed bundle object with an `entries` list -- a
# malformed body would let a future restore drill replay nothing and look
# successful.
if ! /opt/personal-agent/.venv/bin/python -c \
  "import json; body=json.load(open('$DELETION_MANIFEST')); assert isinstance(body, dict) and isinstance(body.get('entries'), list)"; then
  echo "FAIL: deletion-manifest export is not a sealed bundle entries object at $DELETION_MANIFEST" >&2
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

# DEV-036: write the backup-success marker the health check reads. Only after
# `restic check` passes, so the marker's presence means a backup landed in OSS
# *and* verified there -- not merely that the script ran. The content is one
# RFC 3339 UTC timestamp; the file mtime is deliberately not used, because a
# restore drill that copies the file would refresh the mtime and lie about when
# the last real backup happened. The temp file is renamed on the same filesystem
# so the observer sees either the old complete timestamp or the new one, never a
# truncated `install(1)` destination.
tmp=$(mktemp "$BACKUP_STATE_DIR/.last-successful-backup.XXXXXX")
cleanup_marker_tmp() { rm -f "$tmp"; }
trap cleanup_marker_tmp EXIT
date -u +%Y-%m-%dT%H:%M:%SZ > "$tmp"
chmod 0644 "$tmp"
mv -f "$tmp" "$MARKER"
trap - EXIT

echo "== DEV-035 backup OK =="
