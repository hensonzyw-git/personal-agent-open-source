#!/usr/bin/env bash
# DAL-R08: the production operator-token issuing channel. Runs ON the ECS as
# deploy and writes the token to a 0600 file in the invoking user's home
# (never stdout, never a log — the token is returned exactly once by design).
#
# This closes the R08 carry-over "production operator-token issuing channel":
# the CLI (personal-agent-dal-console, run from the MacBook Air) authenticates
# with a token issued here against the same service key the DAL service reads.
#
# Usage:
#   bash /opt/personal-agent/deploy/issue_dal_operator_token.sh \
#     --operator-id example-operator --capabilities read control [--hours 1] [--out FILE]
#
# The enrollment secret and service key are read from the root:dal 0640 files;
# this script needs sudo read access to them but the TOKEN never passes through
# any shell argument, environment variable, or log line.

set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
  echo "run as deploy, not root (sudo is used only for reading key files)" >&2
  exit 1
fi

OPERATOR_ID=""
CAPABILITIES=(read)
HOURS=1
OUT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --operator-id) OPERATOR_ID="${2:?}"; shift 2 ;;
    --capabilities)
      shift
      CAPABILITIES=()
      while [ $# -gt 0 ] && [[ "$1" != --* ]]; do
        CAPABILITIES+=("$1"); shift
      done
      ;;
    --hours) HOURS="${2:?}"; shift 2 ;;
    --out) OUT="${2:?}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -n "$OPERATOR_ID" ] || { echo "--operator-id is required" >&2; exit 2; }
[ "${#CAPABILITIES[@]}" -gt 0 ] || { echo "--capabilities needs at least one of read control" >&2; exit 2; }
for cap in "${CAPABILITIES[@]}"; do
  case "$cap" in read|control) ;; *) echo "unknown capability: $cap" >&2; exit 2 ;; esac
done

OUT="${OUT:-$HOME/.dal-operator-token}"
UMASK_GUARD=$(umask)
umask 077

TOKEN=$(sudo -n python3 - "$OPERATOR_ID" "$HOURS" "${CAPABILITIES[@]}" <<'PY'
import base64, hashlib, hmac, json, os, sys, time

operator_id, hours = sys.argv[1], int(sys.argv[2])
capabilities = sys.argv[3:]

key_b64 = open("/etc/personal-agent/dal.env.d/service-key").read().strip()
key = base64.urlsafe_b64decode(key_b64)
payload = {
    "schema_version": "dal.operator-token/1.0",
    "operator_id": operator_id,
    "capabilities": capabilities,
    "issued_at_epoch": int(time.time()),
    "expires_at_epoch": int(time.time()) + hours * 3600,
}
body = base64.urlsafe_b64encode(
    json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
).decode().rstrip("=")
sig = hmac.new(key, body.encode("ascii"), hashlib.sha256).hexdigest()
print(f"{body}.{sig}")
PY
)

printf '%s' "$TOKEN" > "$OUT"
chmod 0600 "$OUT"
umask "$UMASK_GUARD"

# Report identity and expiry, never the token itself.
python3 - "$OUT" <<'PY'
import base64, json, sys, time

token = open(sys.argv[1]).read().strip()
body = token.split(".", 1)[0]
body += "=" * (-len(body) % 4)
payload = json.loads(base64.urlsafe_b64decode(body))
print(f"token written to {sys.argv[1]} (mode 0600)")
print(f"  operator_id:   {payload['operator_id']}")
print(f"  capabilities:  {', '.join(payload['capabilities'])}")
print(f"  expires_at:    {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(payload['expires_at_epoch']))}")
PY
