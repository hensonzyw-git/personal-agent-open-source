#!/usr/bin/env bash
# DEV-032 acceptance checks. Runs ON the ECS as root, after the services are
# enabled and started. Every check prints PASS or FAIL; the script exits
# non-zero if any check fails. Read-only: it changes nothing.
#
# Acceptance (docs/Phase1开发拆解_v0.1.md Wave 5):
#   两用户不能互读 DB/secret；无 root；个人站无回归。
# Plus the deployment-form checks design 3.2 implies: the API is a Unix
# socket Nginx's group can reach, the MCP is loopback TCP only, and no new
# public port exists.

set -uo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root (sudo bash deploy/verify.sh)" >&2
  exit 1
fi

API_USER=personal-agent-api
MCP_USER=personal-data-mcp
FAILURES=0

pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; FAILURES=$((FAILURES + 1)); }

expect_success() { # <description> <command...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then pass "$desc"; else fail "$desc"; fi
}

expect_refused() { # <description> <command...>; PASS when the command FAILS
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then fail "$desc"; else pass "$desc"; fi
}

echo "== services active, right identity =="
expect_success "personal-data-mcp active" systemctl is-active --quiet personal-data-mcp
expect_success "personal-agent-api active" systemctl is-active --quiet personal-agent-api
expect_success "mcp process runs as $MCP_USER" \
  pgrep -u "$MCP_USER" -f personal-data-mcp
expect_success "api process runs as $API_USER" \
  pgrep -u "$API_USER" -f personal-agent-api
expect_refused "no personal-agent process runs as root" \
  pgrep -u root -f 'personal-agent-api|personal-data-mcp'

echo "== cross-user reads must fail =="
expect_refused "api user cannot read mcp database dir" \
  sudo -u "$API_USER" ls /var/lib/personal-data-mcp
expect_refused "mcp user cannot read api database dir" \
  sudo -u "$MCP_USER" ls /var/lib/personal-agent-api
expect_refused "api user cannot read mcp env" \
  sudo -u "$API_USER" head -c 1 /etc/personal-agent/mcp.env
expect_refused "mcp user cannot read api env" \
  sudo -u "$MCP_USER" head -c 1 /etc/personal-agent/api.env
expect_refused "api user cannot read mcp key dir" \
  sudo -u "$API_USER" ls /etc/personal-agent/keys/mcp
expect_refused "mcp user cannot read api key dir" \
  sudo -u "$MCP_USER" ls /etc/personal-agent/keys/api
expect_refused "mcp user cannot even reach the api socket" \
  sudo -u "$MCP_USER" curl -s --max-time 3 \
  --unix-socket /run/personal-agent/api.sock http://localhost/v1/capabilities

echo "== deployment form (design 3.2) =="
expect_success "api socket exists" test -S /run/personal-agent/api.sock
# uvicorn forces a fresh socket to 0666, so the access boundary is the socket
# *directory*: it must be 0770 personal-agent-api:www-data, which lets exactly
# Nginx's group reach the socket and nobody else.
DIR_OWNER="$(stat -c %U /run/personal-agent)"
DIR_GROUP="$(stat -c %G /run/personal-agent)"
DIR_MODE="$(stat -c %a /run/personal-agent)"
if [ "$DIR_OWNER" = "$API_USER" ] && [ "$DIR_GROUP" = "www-data" ] && [ "$DIR_MODE" -le 770 ]; then
  pass "socket dir $DIR_OWNER:$DIR_GROUP mode $DIR_MODE (Nginx can connect, others cannot)"
else
  fail "socket dir is $DIR_OWNER:$DIR_GROUP/$DIR_MODE, want $API_USER:www-data/<=770"
fi
if [ -S /run/personal-agent/api.sock ]; then
  SOCK_GROUP="$(stat -c %G /run/personal-agent/api.sock)"
  if [ "$SOCK_GROUP" = "www-data" ]; then
    pass "api socket group www-data"
  else
    fail "api socket group is $SOCK_GROUP, want www-data"
  fi
fi

LISTENERS="$(ss -tln)"
if printf '%s\n' "$LISTENERS" | grep -q "127.0.0.1:8811"; then
  pass "mcp bound to 127.0.0.1:8811"
else
  fail "mcp not bound to 127.0.0.1:8811"
fi
if printf '%s\n' "$LISTENERS" | grep -Eq "(0\.0\.0\.0|\*|::):8811"; then
  fail "8811 is reachable on a non-loopback address"
else
  pass "8811 not public"
fi
if printf '%s\n' "$LISTENERS" | grep -q ":8810"; then
  fail "an 8810 TCP listener exists (the API should be UDS only)"
else
  pass "no 8810 TCP listener (API is UDS only)"
fi

echo "== liveness through the real front doors =="
# GET /mcp is refused with 405 by explicit DEV-015 decision — a 405 is proof
# the real server answered, not a proxy or a hung socket.
MCP_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8811/mcp)"
if [ "$MCP_CODE" = "405" ]; then
  pass "mcp GET /mcp -> 405 (expected refusal)"
else
  fail "mcp GET /mcp -> $MCP_CODE, want 405"
fi
# The API requires a device token; an unauthenticated request must be a 401,
# which proves the app answered through the Unix socket.
API_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
  --unix-socket /run/personal-agent/api.sock http://localhost/v1/capabilities)"
if [ "$API_CODE" = "401" ]; then
  pass "api /v1/capabilities over UDS -> 401 (expected refusal)"
else
  fail "api /v1/capabilities over UDS -> $API_CODE, want 401"
fi

echo "== personal site regression =="
SITE_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://zhuyawei.com)"
if [ "$SITE_CODE" = "200" ]; then
  pass "https://zhuyawei.com -> 200"
else
  fail "https://zhuyawei.com -> $SITE_CODE, want 200"
fi

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
else
  echo "$FAILURES CHECK(S) FAILED"
  exit 1
fi
