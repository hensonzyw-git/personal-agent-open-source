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

# DEV-035: a least-privilege backup user. It is a member of both service groups
# so it can READ the staged snapshots, but it never touches the live 0700 data
# directories (those stay owner-only) and never reads a secret. A root backup
# unit would hold both databases and all keys at once; this avoids that.
BACKUP_USER=personal-agent-backup
if id "$BACKUP_USER" >/dev/null 2>&1; then
  echo "user $BACKUP_USER already exists"
else
  useradd --system --no-create-home --shell /usr/sbin/nologin \
    --comment "Personal Agent DEV-035 backup" "$BACKUP_USER"
  echo "created user $BACKUP_USER"
fi
# Group membership gives read access to the staged snapshot dirs below. The
# supplementary groups are also asserted on the real process in verify.sh,
# because sudo resolves groups differently from a systemd unit (see the note
# in deploy/README.md on SupplementaryGroups).
usermod -aG "$API_USER" "$BACKUP_USER" 2>/dev/null || true
usermod -aG "$MCP_USER" "$BACKUP_USER" 2>/dev/null || true

# --- directories --------------------------------------------------------------
# Data: reachable only by the owning service user.
install -d -m 0700 -o "$API_USER" -g "$API_USER" /var/lib/personal-agent-api
install -d -m 0700 -o "$MCP_USER" -g "$MCP_USER" /var/lib/personal-data-mcp

# Environment and key material: root-owned, group-readable by exactly one
# service user, so the service cannot rewrite its own configuration and the
# other service user cannot read it.
install -d -m 0755 -o root -g root /etc/personal-agent
install -d -m 0750 -o root -g "$API_USER" /etc/personal-agent/keys/api
install -d -m 0750 -o root -g "$MCP_USER" /etc/personal-agent/keys/mcp

# Application code: owned by the deploy user; services only read and execute.
install -d -m 0755 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /opt/personal-agent

# DEV-035: staging dirs for the daily snapshots. Each is 0770 owned by its
# service user with the backup user in the group, so the service writes the
# snapshot and the backup user reads it -- and nobody else can. The live data
# dirs above stay 0700, so group membership here grants no access to them.
install -d -m 0770 -o "$API_USER" -g "$BACKUP_USER" /var/backups/personal-agent/api
install -d -m 0770 -o "$MCP_USER" -g "$BACKUP_USER" /var/backups/personal-agent/mcp
# The restic cache is the only path the backup unit writes.
install -d -m 0700 -o "$BACKUP_USER" -g "$BACKUP_USER" /var/cache/restic

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
# The backup script is executed by the backup user from /opt/personal-agent.
install -m 0755 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
  "$(cd "$(dirname "$0")" && pwd)/backup.sh" /opt/personal-agent/deploy/backup.sh
systemctl daemon-reload

echo
echo "Installed. NOT enabled or started. Next: deploy/README.md steps 3-9"
echo "(keys, environment files, ledger config, application, migrations),"
echo "then enable the two services AND personal-data-mcp-observe.timer."
