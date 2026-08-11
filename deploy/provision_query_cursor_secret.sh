#!/usr/bin/env bash
# Add the independent Finance query-cursor key to an already-provisioned ECS.
# This script never prints the key and never restarts either service.

set -euo pipefail
umask 077

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root (sudo bash deploy/provision_query_cursor_secret.sh)" >&2
  exit 1
fi

MCP_USER=personal-data-mcp
MCP_ENV=/etc/personal-agent/mcp.env
LOCK_FILE=/etc/personal-agent/.query-cursor-provision.lock

if [ ! -f "$MCP_ENV" ]; then
  echo "refusing: $MCP_ENV does not exist" >&2
  exit 1
fi
exec 9>"$LOCK_FILE"
if ! flock -w 30 9; then
  echo "refusing: another query cursor provisioning process is still running" >&2
  exit 1
fi
if grep -q '^PERSONAL_DATA_MCP_QUERY_CURSOR_SECRET=' "$MCP_ENV"; then
  echo "query cursor credential already exists; left unchanged"
  exit 0
fi

temporary="$(mktemp /etc/personal-agent/.mcp.env.query.XXXXXX)"
cleanup() {
  rm -f -- "$temporary"
}
trap cleanup EXIT

cp -- "$MCP_ENV" "$temporary"
# Generate and append entirely inside Python: the credential never becomes a
# shell value, argv value or command output, including when invoked by bash -x.
python3 -c 'import base64, os, sys; p=sys.argv[1]; f=open(p, "ab"); f.write(b"\nPERSONAL_DATA_MCP_QUERY_CURSOR_SECRET=" + base64.urlsafe_b64encode(os.urandom(32)) + b"\n"); f.flush(); os.fsync(f.fileno()); f.close()' "$temporary"
chown root:"$MCP_USER" "$temporary"
chmod 0640 "$temporary"
mv -f -- "$temporary" "$MCP_ENV"
python3 -c 'import os, sys; fd=os.open(sys.argv[1], os.O_RDONLY); os.fsync(fd); os.close(fd)' /etc/personal-agent
trap - EXIT

echo "query cursor credential provisioned (secret not printed)"
echo "No service was restarted. Continue with the reviewed deployment procedure."
