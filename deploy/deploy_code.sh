#!/usr/bin/env bash
# DEV-032: deploy the application code to the ECS. Runs ON THE MAC.
#
# The wheel is built from the current checkout and the dependency set is
# exported from uv.lock with hashes, so the server installs exactly what the
# repo pins — no dependency resolution happens on the server and no GitHub
# credential ever leaves the Mac.
#
# Usage:  bash deploy/deploy_code.sh
# Requires: ssh -i ~/.ssh/personal_agent_example_key deploy@192.0.2.10 working.
#
# It does NOT restart the services: the runbook (deploy/README.md) owns the
# order of migrations and restarts, and a code push that silently restarted a
# writing service would be a surprise, not a convenience.

set -euo pipefail

ECS_HOST=deploy@192.0.2.10
ECS_SSH=(ssh -i ~/.ssh/personal_agent_example_key "$ECS_HOST")
ECS_SCP=(scp -i ~/.ssh/personal_agent_example_key)
REMOTE=/opt/personal-agent

cd "$(dirname "$0")/.."

rm -rf dist
uv build --wheel >/dev/null
uv export --locked --format requirements-txt --extra adk --no-dev \
  --no-emit-project -o dist/requirements.txt
WHEEL="$(ls dist/personal_agent-*.whl | head -1)"
echo "built $WHEEL and dist/requirements.txt"

"${ECS_SSH[@]}" "mkdir -p $REMOTE/releases"
"${ECS_SCP[@]}" "$WHEEL" dist/requirements.txt "$ECS_HOST:$REMOTE/releases/"
"${ECS_SCP[@]}" deploy/operator-cli.sh "$ECS_HOST:$REMOTE/operator-cli.sh"
"${ECS_SSH[@]}" "chmod 0755 $REMOTE/operator-cli.sh"

"${ECS_SSH[@]}" bash -s <<EOF
set -euo pipefail
cd $REMOTE
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --quiet --require-hashes -r releases/requirements.txt
.venv/bin/pip install --quiet --no-deps --force-reinstall releases/$(basename "$WHEEL")
.venv/bin/personal-agent-api --help >/dev/null
.venv/bin/personal-data-mcp --help >/dev/null
echo "installed: \$(.venv/bin/pip show personal-agent | head -2 | tail -1)"
EOF

echo
echo "Deployed to $ECS_HOST:$REMOTE (services NOT restarted)."
echo "First rollout: continue with deploy/README.md step 7 (migrations, enable)."
echo "Upgrade: sudo systemctl restart personal-data-mcp personal-agent-api"
