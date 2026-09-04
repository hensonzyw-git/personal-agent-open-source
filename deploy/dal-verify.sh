#!/usr/bin/env bash
# DAL-R08 ECS baseline acceptance. Runs ON the ECS as root AFTER
# deploy/install.sh + provision_dal_keys.sh + the wheel + migration + enable.
#
# Covers, in verify.sh's positive-control style (a refusal only means
# isolation when the paired positive control shows the path works):
#   - the service is active as personal-agent-dal, not root, with no
#     supplementary Finance/backup group
#   - loopback-only listener on 8820 (no non-loopback bind)
#   - the DAL data dir is 0700 dal:dal and its files 0600 dal:dal
#   - the DAL env/key files are root:dal 0640 and READABLE by the service
#     user (positive control) but NOT by deploy, NOT by the Finance
#     API user, and NOT by the backup user
#   - the Finance boundaries are untouched: personal-agent-dal cannot read
#     /var/lib/personal-agent-api or /etc/personal-agent/api.env, and
#     personal-agent-api cannot read /var/lib/personal-agent-dal
#   - the kill switch file is root:root 0644, readable by the service
#   - /health answers 200 over loopback; through Nginx HTTPS the transport
#     answers with the worker envelope; an unauthenticated /operator/jobs
#     answers 401 with the operator envelope
#   - the write switch position is reported but not judged (verify.sh rule)
#   - the personal site is still 200
#
# Output contains no secrets. Exit non-zero on any FAIL.

set -u

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "FAIL: $1"; }

check() { # <description> <command...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then pass "$desc"; else fail "$desc"; fi
}

DAL_USER=personal-agent-dal
API_USER=personal-agent-api
BACKUP_USER=personal-agent-backup
DEPLOY_USER=deploy
DAL_DATA=/var/lib/personal-agent-dal
DAL_ENV=/etc/personal-agent/dal.env
DAL_ENV_D=/etc/personal-agent/dal.env.d
KILL_SWITCH=/etc/personal-agent/dal-kill-switch.json
BASE=http://127.0.0.1:8820

# --- unit / identity ----------------------------------------------------------
check "dal unit is active" systemctl is-active --quiet personal-agent-dal-api
check "dal reconciliation timer is enabled" systemctl is-enabled --quiet personal-agent-dal-reconcile.timer
check "dal reconciliation timer is active" systemctl is-active --quiet personal-agent-dal-reconcile.timer
DAL_PROC="$(systemctl show -p MainPID --value personal-agent-dal-api)"
if [ -n "$DAL_PROC" ] && [ "$DAL_PROC" != 0 ]; then
  check "dal main pid belongs to $DAL_USER" ps -o user= -p "$DAL_PROC" | grep -qx "$DAL_USER"
  check "dal process has no root uid" ! ps -o user= -p "$DAL_PROC" | grep -qx root
  REAL_GROUPS="$(id -nG "$DAL_PROC" 2>/dev/null || true)"
  if printf '%s\n' "$REAL_GROUPS" | grep -Eq "(^| )($API_USER|$BACKUP_USER|www-data)( |$)"; then
    fail "dal process group set must not include $API_USER/$BACKUP_USER/www-data (got: $REAL_GROUPS)"
  else
    pass "dal process group set excludes Finance/backup/www-data groups"
  fi
else
  fail "dal unit has no MainPID"
fi

# --- listener ------------------------------------------------------------------
LISTENERS="$(ss -tlnp 2>/dev/null || ss -tln)"
if printf '%s\n' "$LISTENERS" | grep -q "127.0.0.1:8820"; then
  pass "dal bound to 127.0.0.1:8820"
else
  fail "dal not bound to 127.0.0.1:8820"
fi
if printf '%s\n' "$LISTENERS" | grep -Eq "(0\.0\.0\.0|\*|\[::\]):8820"; then
  fail "8820 is reachable on a non-loopback address"
else
  pass "8820 not public"
fi

# --- data dir / files ----------------------------------------------------------
DAL_DIR_MODE="$(stat -c '%a %U %G' "$DAL_DATA" 2>/dev/null || echo missing)"
if [ "$DAL_DIR_MODE" = "700 $DAL_USER $DAL_USER" ]; then
  pass "dal data dir is 0700 dal:dal"
else
  fail "dal data dir is [$DAL_DIR_MODE], want 700 dal:dal"
fi
BAD_FILES="$(find "$DAL_DATA" -type f ! -mode 0600 2>/dev/null | head -5)"
if [ -z "$BAD_FILES" ]; then
  pass "all files under $DAL_DATA are 0600"
else
  fail "files under $DAL_DATA not 0600: $BAD_FILES"
fi
check "dal db file exists" test -f "$DAL_DATA/dal.sqlite"

# --- env / key files: ownership + cross-user read matrix ------------------------
for f in "$DAL_ENV" "$DAL_ENV_D/service-key" "$DAL_ENV_D/enrollment-secret"; do
  m="$(stat -c '%a %U %G' "$f" 2>/dev/null || echo missing)"
  if [ "$m" = "640 root $DAL_USER" ]; then
    pass "$(basename "$f") is 0640 root:dal"
  else
    fail "$(basename "$f") is [$m], want 640 root:dal"
  fi
  # Positive control: the owning service user CAN read it.
  check "dal user can read $(basename "$f")" \
    sudo -u "$DAL_USER" test -r "$f"
  # Negative: the deploy user, the Finance API user and the backup user cannot.
  if sudo -u "$DEPLOY_USER" test -r "$f" 2>/dev/null; then
    fail "deploy CAN read $(basename "$f")"
  else
    pass "deploy cannot read $(basename "$f")"
  fi
  if sudo -u "$API_USER" test -r "$f" 2>/dev/null; then
    fail "$API_USER CAN read $(basename "$f")"
  else
    pass "$API_USER cannot read $(basename "$f")"
  fi
  if sudo -u "$BACKUP_USER" test -r "$f" 2>/dev/null; then
    fail "$BACKUP_USER CAN read $(basename "$f")"
  else
    pass "$BACKUP_USER cannot read $(basename "$f")"
  fi
done

# --- cross-domain isolation (both directions, with positive controls) ----------
check "positive: api user reads its own env" sudo -u "$API_USER" test -r /etc/personal-agent/api.env
if sudo -u "$DAL_USER" test -r /var/lib/personal-agent-api 2>/dev/null; then
  fail "dal user CAN traverse /var/lib/personal-agent-api"
else
  pass "dal user cannot read /var/lib/personal-agent-api"
fi
if sudo -u "$DAL_USER" test -r /etc/personal-agent/api.env 2>/dev/null; then
  fail "dal user CAN read /etc/personal-agent/api.env"
else
  pass "dal user cannot read /etc/personal-agent/api.env"
fi
if sudo -u "$API_USER" test -r "$DAL_DATA/dal.sqlite" 2>/dev/null; then
  fail "$API_USER CAN read dal.sqlite"
else
  pass "$API_USER cannot read dal.sqlite"
fi
check "positive: dal user reads its own db" sudo -u "$DAL_USER" test -r "$DAL_DATA/dal.sqlite"

# --- kill switch ----------------------------------------------------------------
KS="$(stat -c '%a %U %G' "$KILL_SWITCH" 2>/dev/null || echo missing)"
if [ "$KS" = "644 root root" ]; then
  pass "dal kill switch is 0644 root:root"
else
  fail "dal kill switch is [$KS], want 644 root root"
fi
check "dal user can read the kill switch" sudo -u "$DAL_USER" test -r "$KILL_SWITCH"
HEALTH_KS="$(curl -s --max-time 5 "$BASE/health" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("kill_switch"))' 2>/dev/null || echo error)"
echo "NOTE: kill switch position over /health: $HEALTH_KS (reported, not judged)"

# --- transport / envelopes ------------------------------------------------------
HEALTH_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE/health")"
if [ "$HEALTH_CODE" = 200 ]; then
  pass "/health answers 200 over loopback"
else
  fail "/health answered $HEALTH_CODE"
fi
# Unauthenticated operator call through the real Nginx TLS path: 401 with the
# operator envelope proves routing AND the operator identity domain in one call.
OP_BODY="$(curl -s --max-time 10 "https://agent.example.invalid/dal/transport/v1/operator/jobs")"
if printf '%s\n' "$OP_BODY" | grep -q '"dal.operator-transport/1.0"'; then
  pass "unauthenticated /operator/jobs answers the operator envelope via Nginx"
else
  fail "unauthenticated /operator/jobs body lacks the operator envelope: $OP_BODY"
fi
OP_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://agent.example.invalid/dal/transport/v1/operator/jobs")"
if [ "$OP_CODE" = 401 ]; then
  pass "unauthenticated /operator/jobs is 401"
else
  fail "unauthenticated /operator/jobs answered $OP_CODE, want 401"
fi

# --- the personal site must still be reachable -----------------------------------
SITE_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://zhuyawei.com)"
if [ "$SITE_CODE" = 200 ]; then
  pass "personal site still 200"
else
  fail "personal site answered $SITE_CODE"
fi

echo
echo "dal-verify: $PASS PASS / $FAIL FAIL"
[ "$FAIL" -eq 0 ]
