#!/usr/bin/env bash
# DEV-032: run an Agent operator CLI (personal-agent-device, personal-agent-db,
# personal-agent-review, ...) on the ECS as the service user with the service
# environment. Lives on the ECS at /opt/personal-agent/operator-cli.sh.
#
# The data directory is personal-agent-api:personal-agent-api mode 0700, so an
# operator running as the deploy user has no path to the database at all — every
# operator command goes through this wrapper, which is also what keeps the
# audit trail honest about which identity touched the database.
#
# Usage (as the configured deploy user):
#   /opt/personal-agent/operator-cli.sh personal-agent-device \
#     --database /var/lib/personal-agent-api/agent.sqlite list
#   /opt/personal-agent/operator-cli.sh personal-agent-device \
#     --database /var/lib/personal-agent-api/agent.sqlite issue-code

set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: $0 <cli-command> [args...]" >&2
  exit 2
fi

# The env file is *parsed*, never sourced. `. api.env` would hand the file to
# the shell, so a value containing `$(...)`, a backtick or a `#` would be
# executed or truncated — and systemd's own EnvironmentFile parser does none of
# that. Two parsers disagreeing about the same file means the operator CLI and
# the service can hold different values for the same API key, which is exactly
# the kind of divergence that is invisible until a write goes to the wrong
# place. This loop reads plain `NAME=value` lines literally, like systemd does
# for the unquoted values provision_server_keys.sh writes.
exec sudo -u personal-agent-api bash -c '
  set -euo pipefail
  # sudo gives the service user a bare PATH without the venv; the CLI names
  # (personal-agent-device, ...) live in /opt/personal-agent/.venv/bin.
  export PATH=/opt/personal-agent/.venv/bin:/usr/local/bin:/usr/bin:/bin
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ""|"#"*) continue ;; esac
    name=${line%%=*}
    case "$name" in
      "$line") continue ;;                 # no "=" at all: not an assignment
      ""|*[!A-Za-z0-9_]*) continue ;;      # not a shell-safe variable name
    esac
    export "$name=${line#*=}"
  done < /etc/personal-agent/api.env
  exec "$@"
' bash "$@"
