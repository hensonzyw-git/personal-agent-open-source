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
systemctl daemon-reload

echo
echo "Installed. NOT enabled or started. Next: deploy/README.md steps 3-9"
echo "(keys, environment files, ledger config, application, migrations),"
echo "then 'systemctl enable --now personal-data-mcp personal-agent-api'."
