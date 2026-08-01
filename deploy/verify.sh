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

# A refusal check alone proves nothing: `head` on a file that does not exist and
# `ls` on a missing directory both fail, so a deployment that never created the
# secret would report every cross-user check as PASS. Each refusal below is
# therefore paired with the positive control that the *owning* user can read the
# same path — the pair is what makes "cannot read" mean isolation rather than
# absence.
expect_isolated() { # <what> <owner-user> <other-user> <read-command...>
  local what="$1" owner="$2" other="$3"; shift 3
  expect_success "$owner CAN read $what (control)" sudo -u "$owner" "$@"
  expect_refused "$other cannot read $what" sudo -u "$other" "$@"
}

echo "== services active, right identity =="
expect_success "personal-data-mcp active" systemctl is-active --quiet personal-data-mcp
expect_success "personal-agent-api active" systemctl is-active --quiet personal-agent-api
expect_success "DEV-034 observe timer enabled" \
  systemctl is-enabled --quiet personal-data-mcp-observe.timer
expect_success "DEV-034 observe timer active" \
  systemctl is-active --quiet personal-data-mcp-observe.timer
OBSERVE_RESULT="$(systemctl show -p Result --value personal-data-mcp-observe.service 2>/dev/null)"
OBSERVE_STARTED="$(systemctl show -p ExecMainStartTimestampMonotonic --value personal-data-mcp-observe.service 2>/dev/null)"
if [ "$OBSERVE_RESULT" = "success" ] \
   && [ -n "$OBSERVE_STARTED" ] && [ "$OBSERVE_STARTED" != "0" ]; then
  pass "DEV-034 immediate observe run succeeded"
else
  fail "DEV-034 observe service was not run successfully (result=${OBSERVE_RESULT:-unknown}, started=${OBSERVE_STARTED:-0})"
fi
expect_success "mcp process runs as $MCP_USER" \
  pgrep -u "$MCP_USER" -f personal-data-mcp
expect_success "api process runs as $API_USER" \
  pgrep -u "$API_USER" -f personal-agent-api
expect_refused "no personal-agent process runs as root" \
  pgrep -u root -f 'personal-agent-api|personal-data-mcp'

echo "== the running services hold the credentials they need =="
# `sudo -u` below resolves groups from the passwd primary gid, which is NOT what
# the units run with: the API overrides its primary group to www-data so Nginx
# can reach the socket, and only `SupplementaryGroups=` puts personal-agent-api
# back. Assert the real process credential rather than inferring it from a sudo
# shell that has a different group set.
api_pid="$(systemctl show -p MainPID --value personal-agent-api 2>/dev/null)"
if [ -n "$api_pid" ] && [ "$api_pid" != "0" ] && [ -r "/proc/$api_pid/status" ]; then
  # `Groups:` is the supplementary list; `Gid:` holds real/effective/saved/fs.
  # Either is enough to reach the key ring, so both are collected — asserting
  # only the supplementary list would fail a future unit that made
  # personal-agent-api the primary group again.
  API_GIDS="$(awk '/^(Groups|Gid):/{$1=""; print}' "/proc/$api_pid/status")"
  API_KEY_GID="$(stat -c %g /etc/personal-agent/keys/api)"
  if printf '%s\n' "$API_GIDS" | tr ' \t' '\n\n' | grep -qx "$API_KEY_GID"; then
    pass "api process holds gid $API_KEY_GID, so it can read its own key ring"
  else
    fail "api process groups ($API_GIDS) lack gid $API_KEY_GID; it cannot read /etc/personal-agent/keys/api"
  fi
else
  fail "could not read the api process credentials from /proc"
fi

echo "== cross-user reads must fail (each with its positive control) =="
expect_isolated "the mcp database dir" "$MCP_USER" "$API_USER" \
  ls /var/lib/personal-data-mcp
expect_isolated "the api database dir" "$API_USER" "$MCP_USER" \
  ls /var/lib/personal-agent-api
expect_isolated "the mcp env" "$MCP_USER" "$API_USER" \
  head -c 1 /etc/personal-agent/mcp.env
expect_isolated "the api env" "$API_USER" "$MCP_USER" \
  head -c 1 /etc/personal-agent/api.env
expect_isolated "the mcp key dir" "$MCP_USER" "$API_USER" \
  ls /etc/personal-agent/keys/mcp
expect_isolated "the api key dir" "$API_USER" "$MCP_USER" \
  ls /etc/personal-agent/keys/api
# The socket has no owning *user* to control against — reaching it is Nginx's
# privilege, and www-data is not one of the two service users. The control is
# therefore that the socket answers at all, which the liveness section proves.
expect_refused "mcp user cannot even reach the api socket" \
  sudo -u "$MCP_USER" curl -s --max-time 3 \
  --unix-socket /run/personal-agent/api.sock http://localhost/v1/capabilities

echo "== deployment form (design 3.2) =="
expect_success "api socket exists" test -S /run/personal-agent/api.sock
# uvicorn forces a fresh socket to 0666, so the access boundary is the socket
# *directory*: it must be 0770 personal-agent-api:www-data, which lets exactly
# Nginx's group reach the socket and nobody else.
#
# The mode is compared digit by digit, never numerically. `stat -c %a` prints
# octal, and `[ "$m" -le 770 ]` reads it as decimal: 0755 and 0707 both compare
# as "<= 770" and pass, yet both grant `other` the execute bit that lets any
# user on the box traverse into this directory and connect to the 0666 socket.
# The only property worth asserting is that `other` has nothing.
DIR_OWNER="$(stat -c %U /run/personal-agent)"
DIR_GROUP="$(stat -c %G /run/personal-agent)"
DIR_MODE="$(stat -c %a /run/personal-agent)"
DIR_OTHER="${DIR_MODE: -1}"
if [ "$DIR_OWNER" = "$API_USER" ] && [ "$DIR_GROUP" = "www-data" ] \
   && [ "$DIR_OTHER" = "0" ]; then
  pass "socket dir $DIR_OWNER:$DIR_GROUP mode $DIR_MODE (Nginx can connect, others cannot)"
else
  fail "socket dir is $DIR_OWNER:$DIR_GROUP/$DIR_MODE, want $API_USER:www-data with no 'other' bits"
fi
if [ -S /run/personal-agent/api.sock ]; then
  SOCK_GROUP="$(stat -c %G /run/personal-agent/api.sock)"
  if [ "$SOCK_GROUP" = "www-data" ]; then
    pass "api socket group www-data"
  else
    fail "api socket group is $SOCK_GROUP, want www-data"
  fi
fi

# The database files themselves, not just the directory. The API runs with
# `Group=www-data` so Nginx can reach the socket, which means every file it
# creates is group www-data; only `UMask=0077` keeps Nginx off the ledger's
# Agent-side database, and the 0700 directory is then the second layer rather
# than the only one. Nothing asserted this before, so a umask regression would
# have been invisible.
for data_dir in /var/lib/personal-agent-api /var/lib/personal-data-mcp; do
  bad=""
  for data_file in "$data_dir"/*; do
    [ -f "$data_file" ] || continue
    mode="$(stat -c %a "$data_file")"
    # `stat -c %a` drops leading zeros, so pad before slicing: a 000-mode file
    # prints as "0" and would otherwise read as a group digit of "0" and an
    # other digit that is not there.
    while [ "${#mode}" -lt 3 ]; do mode="0$mode"; done
    # Compare the group and other digits as digits, never numerically.
    case "${mode: -2}" in
      00) ;;
      *) bad="$bad $(basename "$data_file"):$mode" ;;
    esac
  done
  if [ -z "$bad" ]; then
    pass "no group/other access on any file in $data_dir"
  else
    fail "files in $data_dir grant group or other access:$bad"
  fi
done

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

echo "== DEV-035 backup isolation and units =="
BACKUP_USER=personal-agent-backup
# Staging is shared through the backup user's own group. Joining either service
# group would also expose that service's 0640 env and keys, collapsing the data-
# key / repository-key separation DEV-035 is meant to preserve.
BACKUP_GROUPS="$(id -nG "$BACKUP_USER")"
for forbidden_group in "$API_USER" "$MCP_USER"; do
  if printf '%s\n' "$BACKUP_GROUPS" | tr ' ' '\n' | grep -Fxq "$forbidden_group"; then
    fail "$BACKUP_USER unexpectedly belongs to $forbidden_group"
  else
    pass "$BACKUP_USER does not belong to $forbidden_group"
  fi
done
UNIT_SUPPLEMENTARY="$(systemctl show personal-agent-backup.service -p SupplementaryGroups --value)"
if [ -z "$UNIT_SUPPLEMENTARY" ]; then
  pass "backup unit declares no supplementary service groups"
else
  fail "backup unit declares supplementary groups: $UNIT_SUPPLEMENTARY"
fi

expect_isolated "the live api database dir" "$API_USER" "$BACKUP_USER" \
  ls /var/lib/personal-agent-api
expect_isolated "the live mcp database dir" "$MCP_USER" "$BACKUP_USER" \
  ls /var/lib/personal-data-mcp
expect_isolated "the api env from the backup user" "$API_USER" "$BACKUP_USER" \
  head -c 1 /etc/personal-agent/api.env
expect_isolated "the mcp env from the backup user" "$MCP_USER" "$BACKUP_USER" \
  head -c 1 /etc/personal-agent/mcp.env
expect_isolated "the api key dir from the backup user" "$API_USER" "$BACKUP_USER" \
  ls /etc/personal-agent/keys/api
expect_isolated "the mcp key dir from the backup user" "$MCP_USER" "$BACKUP_USER" \
  ls /etc/personal-agent/keys/mcp
# Positive control: the backup user CAN read the staging dirs (so the refusal
# above is isolation, not absence of the path).
expect_success "backup user can read api staging dir" \
  sudo -u "$BACKUP_USER" ls /var/backups/personal-agent/api
expect_success "backup user can read mcp staging dir" \
  sudo -u "$BACKUP_USER" ls /var/backups/personal-agent/mcp
# Listing a directory is not opening a file, and on 2026-08-01 that difference
# was the whole defect: the dirs were 0770 group=backup and listed fine, while
# every staged file inside was 0600 owner-only under UMask=0077. The backup ran,
# stat'd all three inputs, and died on its first actual read. Assert the read.
for pair in "agent.latest.sqlite:api" "deletion-manifest.json:api" \
            "finance.latest.sqlite:mcp"; do
  f="${pair%%:*}"; sub="${pair##*:}"
  p=/var/backups/personal-agent/$sub/$f
  if [ ! -e "$p" ]; then
    fail "staged file $p does not exist (has the db-backup unit run?)"
  else
    expect_success "backup user can OPEN staged $sub/$f" \
      sudo -u "$BACKUP_USER" head -c 1 "$p"
  fi
done
# Staging dirs: 2770, owned by the service user, group backup, no 'other'. The
# setgid bit is what makes new staged files inherit the backup group.
for pair in "$API_USER:api" "$MCP_USER:mcp"; do
  owner="${pair%%:*}"; sub="${pair##*:}"
  d=/var/backups/personal-agent/$sub
  m="$(stat -c %a "$d")"; while [ "${#m}" -lt 4 ]; do m="0$m"; done
  if [ "$(stat -c %U "$d")" = "$owner" ] && [ "$(stat -c %G "$d")" = "$BACKUP_USER" ] \
     && [ "${m: -1}" = "0" ] && [ "${m:0:1}" = "2" ]; then
    pass "staging dir $d is $owner:$BACKUP_USER/$m"
  else
    fail "staging dir $d is $(stat -c %U:%G "$d")/$m, want $owner:$BACKUP_USER setgid (2770) with no 'other'"
  fi
done
expect_success "personal-agent-backup.timer enabled" \
  systemctl is-enabled --quiet personal-agent-backup.timer
expect_success "personal-agent-db-backup.timer enabled" \
  systemctl is-enabled --quiet personal-agent-db-backup.timer
expect_success "personal-data-mcp-db-backup.timer enabled" \
  systemctl is-enabled --quiet personal-data-mcp-db-backup.timer
# restic.env is root:backup 0640 so the backup user can read OSS creds. The repo
# password file must be readable by the SAME user -- restic opens it under the
# unit's User=, so a root-only key fails closed at run time instead of being
# safer. Both stay unreadable to the api and mcp users.
if [ -f /etc/personal-agent/restic.env ]; then
  re_mode="$(stat -c %a /etc/personal-agent/restic.env)"; while [ "${#re_mode}" -lt 3 ]; do re_mode="0$re_mode"; done
  if [ "$(stat -c %G /etc/personal-agent/restic.env)" = "$BACKUP_USER" ] \
     && [ "${re_mode: -1}" = "0" ]; then
    pass "restic.env is root:$BACKUP_USER/$re_mode"
  else
    fail "restic.env is root:$(stat -c %G /etc/personal-agent/restic.env)/$re_mode, want root:$BACKUP_USER with no 'other'"
  fi
  expect_refused "api user cannot read restic.env" \
    sudo -u "$API_USER" head -c 1 /etc/personal-agent/restic.env
  expect_refused "mcp user cannot read restic.env" \
    sudo -u "$MCP_USER" head -c 1 /etc/personal-agent/restic.env
  # The gap that let a root-only key ship on 2026-08-01: restic.env's own mode
  # was asserted, the key it points at was not, so the unit was one run away
  # from EACCES while every check stayed green. Never print the value.
  PW_FILE="$(sed -n 's/^RESTIC_PASSWORD_FILE=//p' /etc/personal-agent/restic.env | tail -1)"
  if [ -z "$PW_FILE" ]; then
    fail "restic.env has no RESTIC_PASSWORD_FILE"
  elif [ ! -f "$PW_FILE" ]; then
    fail "RESTIC_PASSWORD_FILE points at a missing file"
  else
    expect_success "backup user can read the repo password file" \
      sudo -u "$BACKUP_USER" test -r "$PW_FILE"
    expect_refused "api user cannot read the repo password file" \
      sudo -u "$API_USER" head -c 1 "$PW_FILE"
    expect_refused "mcp user cannot read the repo password file" \
      sudo -u "$MCP_USER" head -c 1 "$PW_FILE"
    pw_mode="$(stat -c %a "$PW_FILE")"; while [ "${#pw_mode}" -lt 3 ]; do pw_mode="0$pw_mode"; done
    if [ "${pw_mode: -1}" = "0" ]; then
      pass "repo password file has no 'other' access ($pw_mode)"
    else
      fail "repo password file is world-accessible ($pw_mode)"
    fi
  fi
fi

echo "== operator cli =="
# The wrapper is the only honest path to the database for an operator. sudo
# hands the service user a bare PATH, so a wrapper that forgets the venv makes
# every operator command die with "not found" while everything above stays
# green — which is exactly how this gap was found on 2026-07-31.
expect_success "operator-cli.sh runs personal-agent-device list" \
  /opt/personal-agent/operator-cli.sh personal-agent-device \
  --database /var/lib/personal-agent-api/agent.sqlite list

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
