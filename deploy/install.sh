#!/usr/bin/env bash
# DEV-032: create the two service users, the directory layout and install the
# systemd units. Runs ON the ECS as root. Idempotent.
#
# This script deliberately does NOT enable or start the services: without key
# material, environment files and the application itself they would fail-closed
# into a restart loop. The README runbook enables them only after every input
# is in place and verified.
#
# It changes nothing about Nginx, the firewall or the personal site.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root (sudo bash deploy/install.sh)" >&2
  exit 1
fi

UNIT_SRC="$(cd "$(dirname "$0")" && pwd)/systemd"
API_USER=personal-agent-api
MCP_USER=personal-data-mcp
DEPLOY_USER=deploy

# --- system users (no home, no shell, no password) ---------------------------
for user in "$API_USER" "$MCP_USER"; do
  if id "$user" >/dev/null 2>&1; then
    echo "user $user already exists"
  else
    useradd --system --no-create-home --shell /usr/sbin/nologin \
      --comment "Personal Agent DEV-032" "$user"
    echo "created user $user"
  fi
done

# DEV-035: a least-privilege backup user. It reads only snapshots whose staging
# directories belong to its own group; it must never join either service group,
# because those groups can read the corresponding env and key directories.
# A root backup unit would hold both databases and all keys at once; this avoids
# that single-compromise boundary.
BACKUP_USER=personal-agent-backup
if id "$BACKUP_USER" >/dev/null 2>&1; then
  echo "user $BACKUP_USER already exists"
else
  useradd --system --no-create-home --shell /usr/sbin/nologin \
    --comment "Personal Agent DEV-035 backup" "$BACKUP_USER"
  echo "created user $BACKUP_USER"
fi
# Remediate boxes installed by the first DEV-035 revision. The staging dirs
# below are group=personal-agent-backup, so these memberships were redundant
# for snapshots and accidentally opened both services' 0640 secrets.
for service_group in "$API_USER" "$MCP_USER"; do
  if id -nG "$BACKUP_USER" | tr ' ' '\n' | grep -Fxq "$service_group"; then
    gpasswd -d "$BACKUP_USER" "$service_group"
    echo "removed $BACKUP_USER from $service_group"
  fi
done

# --- directories --------------------------------------------------------------
# Data: reachable only by the owning service user.
install -d -m 0700 -o "$API_USER" -g "$API_USER" /var/lib/personal-agent-api
install -d -m 0700 -o "$API_USER" -g "$API_USER" \
  /var/lib/personal-agent-api/transcripts
install -d -m 0700 -o "$MCP_USER" -g "$MCP_USER" /var/lib/personal-data-mcp

# Environment and key material: root-owned, group-readable by exactly one
# service user, so the service cannot rewrite its own configuration and the
# other service user cannot read it.
install -d -m 0755 -o root -g root /etc/personal-agent
install -d -m 0750 -o root -g "$API_USER" /etc/personal-agent/keys/api
install -d -m 0750 -o root -g "$MCP_USER" /etc/personal-agent/keys/mcp

# DEV-039: the external-write kill switch. Both services read it on every write,
# so it is world-readable; only root writes it, so a compromised service cannot
# turn its own writes back on. It is created here in the *disabled* position and
# is never overwritten by a reinstall: the switch's position is an operational
# decision, and a deploy that silently re-enabled writes would be exactly the
# silent failure the fail-closed design exists to prevent.
WRITE_SWITCH=/etc/personal-agent/write-switch.json
if [ ! -e "$WRITE_SWITCH" ]; then
  switch_tmp=$(mktemp /etc/personal-agent/.write-switch.XXXXXX)
  cat > "$switch_tmp" <<'SWITCH'
{
  "changed_at": "install",
  "reason": "installed disabled; enable deliberately with personal-agent-write-switch",
  "writes": "disabled"
}
SWITCH
  chown root:root "$switch_tmp"
  chmod 0644 "$switch_tmp"
  mv -n "$switch_tmp" "$WRITE_SWITCH"
  rm -f "$switch_tmp"
fi

# DEV-040 §13.2: the fault breakpoint file is deliberately NOT created here. For
# the write switch, "missing" must mean "writes off"; for the drill pause, a
# missing file is the *disarmed* position (no file means no pause), so a reinstall
# can never resurrect or clobber an armed drill. The operator arms it with
# personal-agent-fault-breakpoint arm --breakpoint ... --seconds ... --reason ...
# and disarms it the same way; absence is the safe default.

# Application code: owned by the deploy user; services only read and execute.
install -d -m 0755 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /opt/personal-agent

# DEV-035: staging dirs for the daily snapshots. The MCP timer still stages a
# file directly, so its directory is setgid 2770. The API media bundle is
# different: only the API may publish or replace its pointer/run, while the
# backup identity can only read it (2750). The live data dirs above stay 0700,
# so membership here grants no access to them.
#
# The setgid bit is load-bearing, not tidiness: without it a file created here
# takes the writer's own primary group (personal-agent-api / personal-data-mcp),
# and the backup user -- who is in neither -- can list the directory but open
# nothing in it. Group inheritance plus the units' UMask=0027 is what actually
# makes the staged files readable; the directory mode alone never did.
install -d -m 2750 -o "$API_USER" -g "$BACKUP_USER" /var/backups/personal-agent/api
install -d -m 2770 -o "$MCP_USER" -g "$BACKUP_USER" /var/backups/personal-agent/mcp
# `install -d` on an existing directory does not reapply the mode, so make the
# permissions and group explicit for boxes provisioned before this change.
chmod 2750 /var/backups/personal-agent/api
chmod 2770 /var/backups/personal-agent/mcp
chgrp "$BACKUP_USER" /var/backups/personal-agent/api /var/backups/personal-agent/mcp
# Multimodal media bundles are prepared by the API identity and consumed by the
# backup identity. The lock is installed by root: neither identity may replace
# it, but both can open it through the group-readable staging directory.
install -d -m 2750 -o "$API_USER" -g "$BACKUP_USER" /var/backups/personal-agent/api/media-runs
chown root:root /var/backups/personal-agent
chmod 0755 /var/backups/personal-agent
# Never reinstall an existing lock: preserve its inode across upgrades.
if [ ! -e /var/backups/personal-agent/media-bundle.lock ]; then
  install -m 0444 -o root -g root /dev/null /var/backups/personal-agent/media-bundle.lock
fi
test ! -L /var/backups/personal-agent/media-bundle.lock
test -f /var/backups/personal-agent/media-bundle.lock
chown root:root /var/backups/personal-agent/media-bundle.lock
chmod 0444 /var/backups/personal-agent/media-bundle.lock
# The restic cache is the only path the backup unit writes.
install -d -m 0700 -o "$BACKUP_USER" -g "$BACKUP_USER" /var/cache/restic
# DEV-036: backup observation state. The backup unit writes the success marker
# here (owner), while the health-check unit reads both timestamps (others).
# 0755/0644 expose only timestamps, not repository credentials. The monitoring
# start is created once and never refreshed by reinstall: otherwise repeatedly
# running install.sh could keep a never-successful backup at INFO forever.
BACKUP_STATE_DIR=/var/lib/personal-agent-backup
BACKUP_MONITOR_START="$BACKUP_STATE_DIR/monitoring-started-at"
install -d -m 0755 -o "$BACKUP_USER" -g "$BACKUP_USER" "$BACKUP_STATE_DIR"
if [ ! -e "$BACKUP_MONITOR_START" ]; then
  monitor_tmp=$(mktemp "$BACKUP_STATE_DIR/.monitoring-started-at.XXXXXX")
  date -u +%Y-%m-%dT%H:%M:%SZ > "$monitor_tmp"
  chown "$BACKUP_USER:$BACKUP_USER" "$monitor_tmp"
  chmod 0644 "$monitor_tmp"
  mv -n "$monitor_tmp" "$BACKUP_MONITOR_START"
  rm -f "$monitor_tmp"
fi

# --- DAL Dev Workflow Service (R05+R08 ECS baseline) ---------------------------
# A separate trust domain from Finance/Agent production: its own nologin user,
# its own 0700 data directory, its own env file. No Nginx group is granted on
# anything: the service listens on 127.0.0.1:8820 and Nginx proxies to it over
# loopback, so there is no shared-socket boundary to manage.
DAL_USER=personal-agent-dal
if id "$DAL_USER" >/dev/null 2>&1; then
  echo "user $DAL_USER already exists"
else
  useradd --system --no-create-home --shell /usr/sbin/nologin \
    --comment "Personal Agent DAL (R08)" "$DAL_USER"
  echo "created user $DAL_USER"
fi
install -d -m 0700 -o "$DAL_USER" -g "$DAL_USER" /var/lib/personal-agent-dal
# R09-B backup-set extension: staging dir (setgid, group=backup) for the DAL
# snapshot the backup user reads, and the snapshot script's libexec home. The
# script imports personal_agent_core, so it runs under the DAL venv.
install -d -m 2770 -o "$DAL_USER" -g "$BACKUP_USER" /var/backups/personal-agent/dal
install -d -m 0755 -o root -g root /opt/personal-agent-dal/libexec
install -m 0755 -o root -g root   "$(cd "$(dirname "$0")" && pwd)/libexec/dal_snapshot.py"   /opt/personal-agent-dal/libexec/dal_snapshot.py
install -m 0644 -o root -g root "$UNIT_SRC/personal-agent-dal-api.service" /etc/systemd/system/
# R09-B backup-set extension: the DAL snapshot unit + timer, installed but not
# enabled (the enablement belongs to the DAL runbook, like every other unit).
install -m 0644 -o root -g root \
  "$UNIT_SRC/personal-agent-dal-db-backup.service" \
  "$UNIT_SRC/personal-agent-dal-db-backup.timer" \
  "$UNIT_SRC/personal-agent-dal-reconcile.service" \
  "$UNIT_SRC/personal-agent-dal-reconcile.timer" /etc/systemd/system/
# Installed but NOT enabled, like every other unit: the env file, key material,
# database and migration do not exist yet, and enabling now would fail-closed
# into a restart loop. deploy/README.md (DAL section) owns the enablement.
systemctl daemon-reload

# --- python runtime -----------------------------------------------------------
if ! python3 -c 'import sys; assert sys.version_info[:2] == (3, 12)' 2>/dev/null; then
  echo "expected Python 3.12 as the system python3 (Ubuntu 24.04)" >&2
  exit 1
fi
if ! python3 -m venv --help >/dev/null 2>&1; then
  echo "installing python3.12-venv"
  apt-get update -qq
  apt-get install -y python3.12-venv
fi

# --- systemd units ------------------------------------------------------------
install -m 0644 -o root -g root "$UNIT_SRC/personal-agent-api.service" /etc/systemd/system/
install -m 0644 -o root -g root "$UNIT_SRC/personal-data-mcp.service" /etc/systemd/system/
# DEV-034 health check. Installed here but, like the services, not enabled:
# enabling it before the application exists would fail every 15 minutes.
install -m 0644 -o root -g root \
  "$UNIT_SRC/personal-data-mcp-observe.service" \
  "$UNIT_SRC/personal-data-mcp-observe.timer" /etc/systemd/system/
# DEV-035 backup units + db-backup snapshot units. Installed but not enabled:
# enabling before restic.env and the application exist would fail daily.
install -m 0644 -o root -g root \
  "$UNIT_SRC/personal-agent-db-backup.service" \
  "$UNIT_SRC/personal-agent-db-backup.timer" \
  "$UNIT_SRC/personal-data-mcp-db-backup.service" \
  "$UNIT_SRC/personal-data-mcp-db-backup.timer" \
  "$UNIT_SRC/personal-agent-backup.service" \
  "$UNIT_SRC/personal-agent-backup.timer" /etc/systemd/system/
# DEV-036 review + cleanup timers. Installed but not enabled: enabling before
# the application and migrations exist would fail daily. Like the observe and
# backup timers, the README runbook enables them once the inputs are in place.
install -m 0644 -o root -g root \
  "$UNIT_SRC/personal-agent-review.service" \
  "$UNIT_SRC/personal-agent-review.timer" \
  "$UNIT_SRC/personal-agent-cleanup.service" \
  "$UNIT_SRC/personal-agent-cleanup.timer" \
  "$UNIT_SRC/personal-agent-media-cleanup.service" \
  "$UNIT_SRC/personal-agent-media-cleanup.timer" /etc/systemd/system/
# The backup script is executed by the backup user from /opt/personal-agent.
# /opt/personal-agent exists (created above), but its deploy/ subdir does not
# until the first DEV-035 install, so create it here.
install -d -m 0755 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /opt/personal-agent/deploy
install -m 0755 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
  "$(cd "$(dirname "$0")" && pwd)/backup.sh" /opt/personal-agent/deploy/backup.sh

echo
echo "Installed. NOT enabled or started. Next: deploy/README.md steps 3-9"
echo "(keys, environment files, ledger config, application, migrations),"
echo "then enable the two services and the observe/review/cleanup/backup timers."
