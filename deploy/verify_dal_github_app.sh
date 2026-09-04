#!/usr/bin/env bash
# R09-B start gate for the GitHub App credential env (2026-09-04 gap: the
# unit file loads /etc/personal-agent/dal.env.d/github-app.env
# unconditionally, so a fresh machine without it failed to boot, and an
# operator could enable the unit with an unfilled template and only see the
# failure in the journal).
#
# Run ON the ECS as root, after provision_dal_keys.sh and after filling in
# github-app.env, BEFORE `systemctl enable --now personal-agent-dal-api`:
#
#   sudo bash deploy/verify_dal_github_app.sh
#
# Checks: the four values are non-empty; the repository is owner/name; the
# key file exists at the configured path and parses as a PEM private key;
# both files are root:personal-agent-dal 0640 (no drift toward group/world
# readability) and readable by the service user. Fails closed: exits
# non-zero on the first missing precondition and prints which one. Prints
# nothing secret — identifiers only, never key bytes.
#
# GITHUB_APP_ENV overrides the env file's path and
# VERIFY_DAL_GITHUB_APP_ALLOW_NON_ROOT=1 skips the root check and the
# service-user readability probes: both exist so the offline shell-contract
# tests can drive the gate against a fixture tree. Production runs set
# neither.
#
# This script makes no network call: a malformed key that openssl accepts is
# still caught by the first real mint, and the adapter's own fail-closed
# refusals cover that. What this gate owns is the configuration surface.

set -euo pipefail

GITHUB_APP_ENV="${GITHUB_APP_ENV:-/etc/personal-agent/dal.env.d/github-app.env}"
DAL_USER="${DAL_USER:-personal-agent-dal}"
ALLOW_NON_ROOT="${VERIFY_DAL_GITHUB_APP_ALLOW_NON_ROOT:-0}"

fail() { echo "FAIL: $*" >&2; exit 1; }

# GNU stat (the ECS) uses -c; BSD stat (a Mac running the offline tests)
# uses -f. Try GNU first, fall back to BSD, so one script serves both.
_file_mode() {
  stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
}
_file_owner() {
  stat -c '%U:%G' "$1" 2>/dev/null || stat -f '%Su:%Sg' "$1"
}

if [ "$(id -u)" -ne 0 ] && [ "$ALLOW_NON_ROOT" != "1" ]; then
  fail "run as root (sudo bash deploy/verify_dal_github_app.sh)"
fi

[ -r "$GITHUB_APP_ENV" ] || fail "$GITHUB_APP_ENV does not exist (run provision_dal_keys.sh, then fill it in)"

# shellcheck disable=SC1090
. "$GITHUB_APP_ENV"

for var in PERSONAL_AGENT_DAL_GITHUB_APP_ID \
           PERSONAL_AGENT_DAL_GITHUB_INSTALLATION_ID \
           PERSONAL_AGENT_DAL_GITHUB_REPOSITORY \
           PERSONAL_AGENT_DAL_GITHUB_PRIVATE_KEY_PATH; do
  val="${!var:-}"
  [ -n "$val" ] || fail "$var is empty in $GITHUB_APP_ENV (fill the template in)"
done

case "$PERSONAL_AGENT_DAL_GITHUB_REPOSITORY" in
  */*) ;;
  *) fail "PERSONAL_AGENT_DAL_GITHUB_REPOSITORY must be owner/name, got $PERSONAL_AGENT_DAL_GITHUB_REPOSITORY" ;;
esac

KEY_PATH="$PERSONAL_AGENT_DAL_GITHUB_PRIVATE_KEY_PATH"
[ -f "$KEY_PATH" ] || fail "the App private key is not at $KEY_PATH (scp it per docs/密钥清单_v0.1.md)"
head -c 32 "$KEY_PATH" | grep -q "PRIVATE KEY" \
  || fail "$KEY_PATH does not start like a PEM private key"
openssl pkey -in "$KEY_PATH" -noout -check >/dev/null 2>&1 \
  || fail "$KEY_PATH does not parse as a private key (openssl refused it)"

_file_mode "$GITHUB_APP_ENV" | grep -q "^640$" \
  || fail "$GITHUB_APP_ENV is mode $(_file_mode "$GITHUB_APP_ENV"), want 640"
_file_mode "$KEY_PATH" | grep -q "^640$" \
  || fail "$KEY_PATH is mode $(_file_mode "$KEY_PATH"), want 640"
if [ "$ALLOW_NON_ROOT" != "1" ]; then
  # Production also pins ownership to root:dal; under the test override the
  # fixture's owner is the test user, so only the mode is asserted there.
  _file_owner "$GITHUB_APP_ENV" | grep -q "^root:$DAL_USER$" \
    || fail "$GITHUB_APP_ENV is $(_file_owner "$GITHUB_APP_ENV"), want root:$DAL_USER"
  _file_owner "$KEY_PATH" | grep -q "^root:$DAL_USER$" \
    || fail "$KEY_PATH is $(_file_owner "$KEY_PATH"), want root:$DAL_USER"

  # The service user must actually be able to read both files: 0640 root:dal
  # only works if the unit's Group= is dal, which is checked here rather than
  # assumed.
  sudo -u "$DAL_USER" head -c 1 "$GITHUB_APP_ENV" >/dev/null 2>&1 \
    || fail "the service user cannot read $GITHUB_APP_ENV"
  sudo -u "$DAL_USER" head -c 1 "$KEY_PATH" >/dev/null 2>&1 \
    || fail "the service user cannot read $KEY_PATH"
fi

echo "OK: github-app.env complete, key parses, mode 640 on both files"
if [ "$ALLOW_NON_ROOT" != "1" ]; then
  echo "    ownership root:$DAL_USER, readable by $DAL_USER"
fi
echo "    next: systemctl enable --now personal-agent-dal-api"
