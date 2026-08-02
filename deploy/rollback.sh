#!/usr/bin/env bash
# DEV-032–036 rollback: stop and remove the installed systemd units. Runs ON
# the ECS as root. It deliberately PRESERVES the service users, all data/state
# under /var/lib/personal-*, backup staging/cache, every secret under
# /etc/personal-agent and the code under /opt/personal-agent — a rollback must
# never destroy state, and wiping any of those is a separate explicit decision.
#
# After removal it re-checks the personal site, because "个人站无回归" is part
# of the acceptance and a rollback is exactly when that assumption moves.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root (sudo bash deploy/rollback.sh)" >&2
  exit 1
fi

systemctl disable --now personal-agent-api personal-data-mcp 2>/dev/null || true
systemctl disable --now \
  personal-data-mcp-observe.timer \
  personal-agent-db-backup.timer \
  personal-data-mcp-db-backup.timer \
  personal-agent-backup.timer \
  personal-agent-review.timer \
  personal-agent-cleanup.timer 2>/dev/null || true
systemctl stop \
  personal-data-mcp-observe.service \
  personal-agent-db-backup.service \
  personal-data-mcp-db-backup.service \
  personal-agent-backup.service \
  personal-agent-review.service \
  personal-agent-cleanup.service 2>/dev/null || true
rm -f \
  /etc/systemd/system/personal-agent-api.service \
  /etc/systemd/system/personal-data-mcp.service \
  /etc/systemd/system/personal-data-mcp-observe.service \
  /etc/systemd/system/personal-data-mcp-observe.timer \
  /etc/systemd/system/personal-agent-db-backup.service \
  /etc/systemd/system/personal-agent-db-backup.timer \
  /etc/systemd/system/personal-data-mcp-db-backup.service \
  /etc/systemd/system/personal-data-mcp-db-backup.timer \
  /etc/systemd/system/personal-agent-backup.service \
  /etc/systemd/system/personal-agent-backup.timer \
  /etc/systemd/system/personal-agent-review.service \
  /etc/systemd/system/personal-agent-review.timer \
  /etc/systemd/system/personal-agent-cleanup.service \
  /etc/systemd/system/personal-agent-cleanup.timer
systemctl daemon-reload

echo "Units removed. PRESERVED (remove only by a separate explicit decision):"
echo "  users: personal-agent-api, personal-data-mcp, personal-agent-backup"
echo "  data/state: /var/lib/personal-agent-api, /var/lib/personal-data-mcp, /var/lib/personal-agent-backup"
echo "  backup staging/cache: /var/backups/personal-agent, /var/cache/restic"
echo "  env/keys: /etc/personal-agent"
echo "  code:  /opt/personal-agent"

SITE_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://zhuyawei.com)"
echo "personal site check: https://zhuyawei.com -> $SITE_CODE"
[ "$SITE_CODE" = "200" ] || exit 1
