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

expect_oneshot_succeeded() { # <unit>
  local unit="$1" result started
  result="$(systemctl show -p Result --value "$unit" 2>/dev/null)"
  started="$(systemctl show -p ExecMainStartTimestampMonotonic --value "$unit" 2>/dev/null)"
  if [ "$result" = "success" ] \
     && [ -n "$started" ] && [ "$started" != "0" ]; then
    pass "$unit ran successfully"
  else
    fail "$unit has no successful run (result=${result:-unknown}, started=${started:-0})"
  fi
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
# A healthy HTTP endpoint is not proof that the newly composed read tool is in
# the catalog. Exercise only MCP initialize + tools/list; this never invokes a
# Finance handler or reads ledger data, and its output is discarded.
expect_success "mcp catalog advertises finance.query_expenses" \
  sudo -u "$MCP_USER" /opt/personal-agent/.venv/bin/python -c '
import asyncio
from personal_agent.mcp_client.core import McpClientCore, StreamableHttpTransport

async def check():
    async with McpClientCore(
        "finance", StreamableHttpTransport(url="http://127.0.0.1:8811/mcp")
    ) as client:
        names = {tool.name for tool in await client.list_tools()}
        if "finance.query_expenses" not in names:
            raise SystemExit(1)

asyncio.run(check())
'
# The calendar domain's tools must be advertised too. `calendar.create_event`
# is a device-executed write: the server advertises it and its handler is a
# fail-closed guard — real execution happens on the phone — so advertising it
# here proves the IR-derived fork deployed, not that any event was written.
expect_success "mcp catalog advertises calendar.create_event and calendar.ingest_events" \
  sudo -u "$MCP_USER" /opt/personal-agent/.venv/bin/python -c '
import asyncio
from personal_agent.mcp_client.core import McpClientCore, StreamableHttpTransport

async def check():
    async with McpClientCore(
        "finance", StreamableHttpTransport(url="http://127.0.0.1:8811/mcp")
    ) as client:
        names = {tool.name for tool in await client.list_tools()}
        missing = {"calendar.create_event", "calendar.ingest_events"} - names
        if missing:
            raise SystemExit(f"missing calendar tools: {sorted(missing)}")

asyncio.run(check())
'
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
# DEV-036: the review and cleanup timers must be enabled, and the backup-age
# marker directory must exist and be traversable by the observe user (others-x)
# without being writable by it (others-w). The marker file itself is written by
# the backup unit; here we only assert the directory contract.
expect_success "personal-agent-review.timer enabled" \
  systemctl is-enabled --quiet personal-agent-review.timer
expect_success "personal-agent-review.timer active" \
  systemctl is-active --quiet personal-agent-review.timer
expect_success "personal-agent-cleanup.timer enabled" \
  systemctl is-enabled --quiet personal-agent-cleanup.timer
expect_success "personal-agent-cleanup.timer active" \
  systemctl is-active --quiet personal-agent-cleanup.timer
expect_oneshot_succeeded personal-agent-review.service
expect_oneshot_succeeded personal-agent-cleanup.service
expect_oneshot_succeeded personal-agent-backup.service
BACKUP_AGE_DIR=/var/lib/personal-agent-backup
if [ -d "$BACKUP_AGE_DIR" ]; then
  state_owner="$(stat -c %U "$BACKUP_AGE_DIR")"
  state_group="$(stat -c %G "$BACKUP_AGE_DIR")"
  state_mode="$(stat -c %a "$BACKUP_AGE_DIR")"
  if [ "$state_owner" = "$BACKUP_USER" ] \
     && [ "$state_group" = "$BACKUP_USER" ] \
     && [ "$state_mode" = "755" ]; then
    pass "backup-age state dir is $state_owner:$state_group/$state_mode"
  else
    fail "backup-age state dir is $state_owner:$state_group/$state_mode, want $BACKUP_USER:$BACKUP_USER/755"
  fi
  expect_refused "observe user cannot write the backup-age dir" \
    sudo -u "$MCP_USER" test -w "$BACKUP_AGE_DIR"
else
  fail "backup-age marker dir $BACKUP_AGE_DIR does not exist (run install.sh)"
fi

# A directory contract alone is not evidence that backup.sh can write through
# its systemd sandbox. The deployment runbook starts the real backup before
# verify.sh; require both timestamp files, their cross-user read boundary and a
# fresh parseable success. Never use mtime: copying/restoring a file changes it.
verify_backup_timestamp() { # <label> <path> <max-age-seconds-or-0>
  local label="$1" path="$2" max_age="$3"
  local owner group mode raw epoch now_epoch age
  if [ ! -f "$path" ]; then
    fail "$label missing at $path"
    return
  fi
  owner="$(stat -c %U "$path")"
  group="$(stat -c %G "$path")"
  mode="$(stat -c %a "$path")"
  if [ "$owner" = "$BACKUP_USER" ] \
     && [ "$group" = "$BACKUP_USER" ] \
     && [ "$mode" = "644" ]; then
    pass "$label is $owner:$group/$mode"
  else
    fail "$label is $owner:$group/$mode, want $BACKUP_USER:$BACKUP_USER/644"
  fi
  expect_success "backup user can read $label" \
    sudo -u "$BACKUP_USER" head -c 1 "$path"
  expect_success "observe user can read $label" \
    sudo -u "$MCP_USER" head -c 1 "$path"
  expect_refused "observe user cannot write $label" \
    sudo -u "$MCP_USER" test -w "$path"

  raw="$(head -n 1 "$path")"
  if ! epoch="$(date -u -d "$raw" +%s 2>/dev/null)"; then
    fail "$label is not an RFC 3339 timestamp"
    return
  fi
  now_epoch="$(date -u +%s)"
  age=$((now_epoch - epoch))
  if [ "$age" -lt -300 ]; then
    fail "$label is more than five minutes in the future"
  elif [ "$max_age" -gt 0 ] && [ "$age" -gt "$max_age" ]; then
    fail "$label is stale (${age}s old, limit ${max_age}s)"
  else
    pass "$label timestamp is parseable and within its allowed age"
  fi
}

verify_backup_timestamp \
  "backup monitoring-start marker" \
  "$BACKUP_AGE_DIR/monitoring-started-at" 0
verify_backup_timestamp \
  "last successful backup marker" \
  "$BACKUP_AGE_DIR/last-successful-backup" 172800
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

echo "== write kill switch =="
# DEV-039. The switch decides whether any external write may happen, so the
# checks are about *reachability and ownership*, not the position: the position
# is an operational decision and either value is legitimate.
#
# Both service users must be able to OPEN it, not merely list its directory --
# the 2026-08-01 backup defect was exactly that distinction, and a switch the
# services cannot read fails every write closed.
SWITCH_FILE=/etc/personal-agent/write-switch.json
# Defined out here, not inside the branch below, because the fault-breakpoint
# block also uses it. Under `set -u` a definition reachable only when the switch
# file exists would abort the whole script at the first later use -- taking the
# remaining checks with it -- in exactly the run where the switch is missing and
# the rest of the report matters most.
#
# Neither service may flip its own switch: a compromised service that could
# rewrite this file could re-enable the writes an operator just stopped.
# `test -w` only asks access(2) what would happen. Open the actual inode for
# append instead, with O_NOFOLLOW, and require the kernel to refuse it. The
# probe closes immediately and writes no bytes even if a regression lets the
# open succeed, so verify.sh remains read-only. The parent-directory mode
# checked below independently prevents unlink/rename replacement.
WRITE_OPEN_PROBE='import os, sys; fd = os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW); os.close(fd)'
if [ ! -f "$SWITCH_FILE" ]; then
  fail "write switch state file is missing; every write will refuse"
else
  sw_mode="$(stat -c %a "$SWITCH_FILE")"; while [ "${#sw_mode}" -lt 3 ]; do sw_mode="0$sw_mode"; done
  sw_owner="$(stat -c %U:%G "$SWITCH_FILE")"
  sw_dir="$(dirname "$SWITCH_FILE")"
  sw_dir_owner="$(stat -c %U:%G "$sw_dir")"
  sw_dir_mode="$(stat -c %a "$sw_dir")"
  if [ "$sw_owner" = "root:root" ] && [ "$sw_mode" = "644" ] \
     && [ "$sw_dir_owner" = "root:root" ] && [ "$sw_dir_mode" = "755" ]; then
    pass "write switch is root:root/0644 under root:root/0755"
  else
    fail "write switch is $sw_owner/$sw_mode under $sw_dir_owner/$sw_dir_mode, want root:root/644 under root:root/755"
  fi
  expect_success "api user can open the write switch" \
    sudo -u "$API_USER" head -c 1 "$SWITCH_FILE"
  expect_success "mcp user can open the write switch" \
    sudo -u "$MCP_USER" head -c 1 "$SWITCH_FILE"
  # The probe's own positive control, and it is not decoration. A refusal-only
  # assertion passes for any reason the command fails: measured on 2026-08-03,
  # a wrong interpreter path and a wrong target path both exit non-zero, which
  # this block would have read as "correctly refused" forever. The adjacent
  # `head -c 1` covers a wrong *target*, but nothing else here runs this
  # interpreter, so a typo in it would silently disarm both checks below.
  # Proving the probe can succeed where writing IS allowed is what makes its
  # refusal mean permission rather than breakage -- the same lesson as the
  # DEV-035 `ls`-on-a-directory defect and the `test -w` this replaced, one
  # level up. The temp file is the probe user's own and is removed immediately.
  expect_success "the write-open probe can actually open a writable file" \
    sudo -u "$MCP_USER" bash -c \
    't=$(mktemp) || exit 1; "$1" -c "$2" "$t"; rc=$?; rm -f "$t"; exit $rc' \
    _ /opt/personal-agent/.venv/bin/python "$WRITE_OPEN_PROBE"
  expect_refused "api process identities cannot open the write switch for writing" \
    bash -c 'sudo -u "$1" "$3" -c "$4" "$5" || sudo -u "$2" "$3" -c "$4" "$5"' \
    _ "$API_USER" www-data /opt/personal-agent/.venv/bin/python \
    "$WRITE_OPEN_PROBE" "$SWITCH_FILE"
  expect_refused "mcp user cannot open the write switch for writing" \
    sudo -u "$MCP_USER" /opt/personal-agent/.venv/bin/python \
    -c "$WRITE_OPEN_PROBE" "$SWITCH_FILE"
  # Parsed by the production reader, not by grep: the services refuse anything
  # this reader refuses, so this is the only check that means what it says.
  /opt/personal-agent/.venv/bin/personal-agent-write-switch \
    --path "$SWITCH_FILE" status >/dev/null 2>&1
  sw_status=$?
  case "$sw_status" in
    0) pass "write switch parses; external writes are ENABLED" ;;
    1) pass "write switch parses; external writes are DISABLED" ;;
    *) fail "write switch state file is unreadable or malformed (exit $sw_status)" ;;
  esac
fi
for unit in personal-agent-api personal-data-mcp; do
  if systemctl show -p Environment --value "$unit" 2>/dev/null \
     | grep -q "PERSONAL_AGENT_WRITE_SWITCH_FILE=$SWITCH_FILE"; then
    pass "$unit points at the write switch"
  else
    fail "$unit does not carry PERSONAL_AGENT_WRITE_SWITCH_FILE=$SWITCH_FILE"
  fi
done

echo "== fault breakpoint (DEV-040 §13.2) =="
# The per-breakpoint drill pause. Absence is the *disarmed* position and is the
# only correct steady state; a present file must be root-owned, readable by the
# Finance user (its only reader), rewrite-proof, and parse through the production
# reader.
#
# Unlike the write switch, the position here is NOT merely an operational
# decision to report. The switch has two legitimate steady states; this file has
# one. A breakpoint left armed after a drill pauses every matching production
# write for the whole arm window, and nothing in the system takes it back down --
# so a routine verify run that printed PASS for "armed" would be applauding the
# exact failure it is here to catch. During an actual drill, export
# ALLOW_ARMED_FAULT_BREAKPOINT=1 to say so deliberately.
FAULT_FILE=/etc/personal-agent/fault-breakpoint.json
if [ ! -f "$FAULT_FILE" ]; then
  pass "no fault breakpoint armed (the disarmed steady state)"
else
  fb_mode="$(stat -c %a "$FAULT_FILE")"; while [ "${#fb_mode}" -lt 3 ]; do fb_mode="0$fb_mode"; done
  fb_owner="$(stat -c %U:%G "$FAULT_FILE")"
  if [ "$fb_owner" = "root:root" ] && [ "$fb_mode" = "644" ]; then
    pass "fault breakpoint is root:root/0644"
  else
    fail "fault breakpoint is $fb_owner/$fb_mode, want root:root/644"
  fi
  expect_success "mcp user can open the fault breakpoint" \
    sudo -u "$MCP_USER" head -c 1 "$FAULT_FILE"
  expect_refused "mcp user cannot open the fault breakpoint for writing" \
    sudo -u "$MCP_USER" /opt/personal-agent/.venv/bin/python \
    -c "$WRITE_OPEN_PROBE" "$FAULT_FILE"
  # Parsed by the production reader, not by grep: a file this reader refuses
  # would silently disarm the drill, turning the operator's kill into a guess.
  /opt/personal-agent/.venv/bin/personal-agent-fault-breakpoint \
    --path "$FAULT_FILE" status >/dev/null 2>&1
  fb_status=$?
  case "$fb_status" in
    0)
      if [ "${ALLOW_ARMED_FAULT_BREAKPOINT:-0}" = "1" ]; then
        pass "fault breakpoint parses and is armed (drill declared)"
      else
        fail "a fault breakpoint is ARMED; production writes will pause. Disarm with: sudo /opt/personal-agent/.venv/bin/personal-agent-fault-breakpoint --path $FAULT_FILE disarm --reason '...'"
      fi
      ;;
    *) fail "fault breakpoint state file is unreadable or malformed (exit $fb_status)" ;;
  esac
fi
if systemctl show -p Environment --value personal-data-mcp 2>/dev/null \
   | grep -q "PERSONAL_AGENT_FAULT_BREAKPOINT_FILE=$FAULT_FILE"; then
  pass "personal-data-mcp points at the fault breakpoint"
else
  fail "personal-data-mcp does not carry PERSONAL_AGENT_FAULT_BREAKPOINT_FILE=$FAULT_FILE"
fi

echo "== push sender (DEV-040) =="
# The review job now composes a real APNs sender, which needs (a) the Agent data
# key to open the sealed device token and (b) either all five APNs variables or
# none of them — a partial set is a typo that must refuse to start, not silently
# fall back to UnavailablePushSender. h2 is a runtime dep of httpx2's HTTP/2; if
# the requirements install dropped it, build_push_sender only fails at send time.
REVIEW_ENV=/etc/personal-agent/api.env
if [ -f "$REVIEW_ENV" ]; then
  apns_present=0
  for v in PERSONAL_AGENT_APNS_KEY_PATH PERSONAL_AGENT_APNS_KEY_ID \
           PERSONAL_AGENT_APNS_TEAM_ID PERSONAL_AGENT_APNS_TOPIC \
           PERSONAL_AGENT_APNS_ENVIRONMENT; do
    if grep -q "^${v}=" "$REVIEW_ENV"; then apns_present=$((apns_present+1)); fi
  done
  case $apns_present in
    0) pass "no APNs variables set; review job keeps UnavailablePushSender (a legitimate choice)" ;;
    5)
      expect_success "the apns auth key is readable by the api user" \
        sudo -u "$API_USER" test -r "$(grep '^PERSONAL_AGENT_APNS_KEY_PATH=' "$REVIEW_ENV" | cut -d= -f2-)"
      ;;
    *) fail "APNs is partially configured ($apns_present of 5 variables); the review job will refuse to start" ;;
  esac
  expect_success "the review job can import its push sender (h2 is installed)" \
    /opt/personal-agent/.venv/bin/python -c "import personal_agent.api.apns, h2"
else
  fail "review service env file $REVIEW_ENV is missing"
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
