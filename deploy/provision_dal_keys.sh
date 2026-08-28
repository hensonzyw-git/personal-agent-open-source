#!/usr/bin/env bash
# DAL-R08 ECS baseline: mint the Dev Workflow Service key material on the ECS.
# Runs ON the ECS as root, after deploy/install.sh. Mirrors
# provision_server_keys.sh but for the DAL service's separate trust domain:
# fresh keys are minted here, never copied from a development Mac, and the
# script refuses to overwrite — rotation is a deliberate act.
#
# Layout (same ownership philosophy as the Finance rings):
#   /etc/personal-agent/dal.env            0640 root:personal-agent-dal
#       paths only today; future non-secret config lines go here
#   /etc/personal-agent/dal.env.d/service-key      0640 root:personal-agent-dal
#       the HMAC service key (worker AND operator tokens share it — same
#       service, two token schemas, one verifying key)
#   /etc/personal-agent/dal.env.d/enrollment-secret 0640 root:personal-agent-dal
#       gates /enroll; travels only to the operator issuing channel, never
#       to a worker
#   /etc/personal-agent/dal-kill-switch.json  0644 root:root, PRESENT
#       the service starts fail-closed: claims and operator mutations answer
#       503 until the file is removed as an explicit go-live action
#
# Nothing here is printed, and no service is restarted.

set -euo pipefail
umask 077

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root (sudo bash deploy/provision_dal_keys.sh)" >&2
  exit 1
fi

DAL_USER=personal-agent-dal
DAL_ENV=/etc/personal-agent/dal.env
DAL_ENV_D=/etc/personal-agent/dal.env.d
KILL_SWITCH=/etc/personal-agent/dal-kill-switch.json

DAL_FILES=(
  "$DAL_ENV"
  "$DAL_ENV_D/service-key"
  "$DAL_ENV_D/enrollment-secret"
)
for path in "${DAL_FILES[@]}"; do
  if [ -e "$path" ]; then
    echo "refusing to overwrite $path; remove it first if you mean to rotate" >&2
    exit 1
  fi
done

install -d -m 0750 -o root -g "$DAL_USER" "$DAL_ENV_D"

python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" \
  > "$DAL_ENV_D/service-key"
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" \
  > "$DAL_ENV_D/enrollment-secret"
chown root:"$DAL_USER" "$DAL_ENV_D/service-key" "$DAL_ENV_D/enrollment-secret"
chmod 0640 "$DAL_ENV_D/service-key" "$DAL_ENV_D/enrollment-secret"

# Env skeleton: paths only, no secrets. Values the operator adds later must
# follow the same rule (no secret belongs in a file the service parses as env).
if [ ! -e "$DAL_ENV" ]; then
  cat > "$DAL_ENV" <<'ENV'
# personal-agent-dal environment. Paths and non-secret configuration only;
# secret material lives in /etc/personal-agent/dal.env.d/ (0640 root:dal).
ENV
  chown root:"$DAL_USER" "$DAL_ENV"
  chmod 0640 "$DAL_ENV"
fi

# The kill switch is laid down PRESENT: the service answers 503
# kill_switch_active on every claim and operator mutation until the file is
# removed deliberately. Unlike the Finance write switch, "absent" here means
# the job queue is OPEN, so a reinstall that recreates the file is the safe
# direction; this block re-arms it on every run.
if [ ! -e "$KILL_SWITCH" ]; then
  switch_tmp=$(mktemp /etc/personal-agent/.dal-kill-switch.XXXXXX)
  cat > "$switch_tmp" <<'SWITCH'
{
  "changed_at": "install",
  "reason": "installed present; go-live is removing this file deliberately"
}
SWITCH
  chown root:root "$switch_tmp"
  chmod 0644 "$switch_tmp"
  mv -n "$switch_tmp" "$KILL_SWITCH"
  rm -f "$switch_tmp"
  echo "kill switch laid down PRESENT (fail-closed)"
fi

echo "DAL key material provisioned under $DAL_ENV_D"
