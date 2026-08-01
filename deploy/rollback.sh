#!/usr/bin/env bash
# DEV-032 rollback: stop and remove the two systemd units. Runs ON the ECS as
# root. It deliberately PRESERVES the service users, all data under
# /var/lib/personal-*, every secret under /etc/personal-agent and the code
# under /opt/personal-agent — a rollback must never destroy state, and wiping
# any of those is a separate, explicit decision.
#
# After removal it re-checks the personal site, because "个人站无回归" is part
# of the acceptance and a rollback is exactly when that assumption moves.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root (sudo bash deploy/rollback.sh)" >&2
  exit 1
fi

systemctl disable --now personal-agent-api personal-data-mcp 2>/dev/null || true
systemctl disable --now personal-data-mcp-observe.timer 2>/dev/null || true
rm -f /etc/systemd/system/personal-agent-api.service
rm -f /etc/systemd/system/personal-data-mcp.service
rm -f /etc/systemd/system/personal-data-mcp-observe.service
rm -f /etc/systemd/system/personal-data-mcp-observe.timer
systemctl daemon-reload

echo "Units removed. PRESERVED (remove only by a separate explicit decision):"
echo "  users: personal-agent-api, personal-data-mcp"
echo "  data:  /var/lib/personal-agent-api, /var/lib/personal-data-mcp"
echo "  env/keys: /etc/personal-agent"
echo "  code:  /opt/personal-agent"

SITE_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://zhuyawei.com)"
echo "personal site check: https://zhuyawei.com -> $SITE_CODE"
[ "$SITE_CODE" = "200" ] || exit 1
