#!/usr/bin/env bash
# DEV-032: run an Agent operator CLI (personal-agent-device, personal-agent-db,
# personal-agent-review, ...) on the ECS as the service user with the service
# environment. Lives on the ECS at /opt/personal-agent/operator-cli.sh.
#
# The data directory is personal-agent-api:personal-agent-api mode 0700, so an
# operator running as deploy has no path to the database at all — every
# operator command goes through this wrapper, which is also what keeps the
# audit trail honest about which identity touched the database.
#
# Usage (as deploy):
#   /opt/personal-agent/operator-cli.sh personal-agent-device \
#     --database /var/lib/personal-agent-api/agent.sqlite list
#   /opt/personal-agent/operator-cli.sh personal-agent-device \
#     --database /var/lib/personal-agent-api/agent.sqlite issue-code

set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: $0 <cli-command> [args...]" >&2
  exit 2
fi

exec sudo -u personal-agent-api \
  bash -c 'set -a; . /etc/personal-agent/api.env; set +a; exec "$@"' bash "$@"
