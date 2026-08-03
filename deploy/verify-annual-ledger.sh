#!/usr/bin/env bash
# Read-only verification against the REAL annual ledger. Runs ON the ECS as root.
#
# This closes the two §13.2 gates that cannot be closed any other way -- annual
# schema validation, and the query pagination Henson reconciles by hand -- under
# his 2026-08-03 staged-G5 decision: read-only production first, writes still
# gated.
#
# It cannot write, and that is structural rather than promised. The tool it calls
# never constructs the write dependencies, never opens the execution store, never
# builds a key ring, and never starts the recovery worker -- recovery being the
# sharp one, since `reconcile_write` calls `create_record` on its own initiative
# to finish a stranded write. See src/personal_data_mcp/finance/verify_ledger_cli.py.
#
# The environment is assembled in one specific order and that order is the point:
# mcp.env supplies the Feishu app credentials, then annual-readonly.env overrides
# only the Base identifiers. The credentials are therefore never duplicated into
# a second file.
#
# Usage (as deploy):
#   sudo bash ~/personal-agent-deploy/verify-annual-ledger.sh freeze
#   sudo bash ~/personal-agent-deploy/verify-annual-ledger.sh schema
#   sudo bash ~/personal-agent-deploy/verify-annual-ledger.sh query [--page-all]

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root (sudo bash $0 <command>)" >&2
  exit 1
fi

VENV=/opt/personal-agent/.venv/bin
MCP_ENV=/etc/personal-agent/mcp.env
ANNUAL_ENV=/etc/personal-agent/annual-readonly.env
CONFIG=/etc/personal-agent/ledger.production.2026.json
LEDGER_YEAR=2026
CONFIG_VERSION=2026.1

for file in "$MCP_ENV" "$ANNUAL_ENV"; do
  [ -r "$file" ] || { echo "cannot read $file" >&2; exit 1; }
done

if grep -q REPLACE_ME "$ANNUAL_ENV"; then
  echo "fill the REPLACE_ME values first:  sudoedit $ANNUAL_ENV" >&2
  grep -n REPLACE_ME "$ANNUAL_ENV" | sed 's/=.*/=REPLACE_ME/' >&2
  exit 1
fi

# Order is load-bearing: credentials first, Base override second.
set -a
# shellcheck disable=SC1090
. "$MCP_ENV"
# shellcheck disable=SC1090
. "$ANNUAL_ENV"
set +a

# A wrong order, or a stale marker, would silently point this at the synthetic
# Base and "verify the annual ledger" against test data. Refuse instead.
if [ "${FEISHU_FINANCE_LEDGER_KIND:-}" != "production" ]; then
  echo "refusing: FEISHU_FINANCE_LEDGER_KIND is '${FEISHU_FINANCE_LEDGER_KIND:-unset}', not 'production'" >&2
  exit 1
fi

case "${1:-}" in
  freeze)
    [ -e "$CONFIG" ] && { echo "refusing: $CONFIG already exists" >&2; exit 1; }
    "$VENV/personal-data-mcp-freeze-ledger" \
      --out "$CONFIG" \
      --ledger-year "$LEDGER_YEAR" \
      --config-version "$CONFIG_VERSION" \
      --ledger-kind production \
      --allow-production-read-only
    chown root:root "$CONFIG"; chmod 0640 "$CONFIG"
    echo "froze $CONFIG"
    ;;
  schema)
    [ -r "$CONFIG" ] || { echo "run 'freeze' first" >&2; exit 1; }
    "$VENV/personal-data-mcp-verify-ledger" --ledger-config "$CONFIG" schema
    ;;
  query)
    [ -r "$CONFIG" ] || { echo "run 'freeze' first" >&2; exit 1; }
    shift
    "$VENV/personal-data-mcp-verify-ledger" --ledger-config "$CONFIG" \
      query --view total \
      --start "$LEDGER_YEAR-01-01" --end "$LEDGER_YEAR-12-31" "$@"
    ;;
  *)
    echo "usage: $0 {freeze|schema|query [--page-all]}" >&2
    exit 2
    ;;
esac
