#!/usr/bin/env bash
# DEV-032: mint the server-side key material on the ECS. Runs ON the ECS as
# root, after deploy/install.sh.
#
# Mirrors scripts/setup_local_agent_keys.sh but for the deployed services, with
# the ownership DEV-032's acceptance requires: every private file is
# root:<service-user> mode 0640, so one service user cannot read the other's
# material and neither can rewrite its own. The public half of the Host-Context
# signing key is the only file that crosses to the Finance side.
#
# Fresh keys are minted here rather than copied from the development Mac: the
# ECS is the G4 environment, its devices enroll against these rings, and key
# material should not travel. Refuses to overwrite anything — rotation is a
# deliberate act.
#
# Also creates the two environment skeletons with the key-ring variables when
# they do not exist yet (values that are paths and KIDs only — no secrets are
# printed). The Feishu / GLM / user-id lines are added by the operator steps
# in deploy/README.md, from the existing local env files, never from chat.

set -euo pipefail
umask 077

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root (sudo bash deploy/provision_server_keys.sh)" >&2
  exit 1
fi

API_USER=personal-agent-api
MCP_USER=personal-data-mcp
API_KEYS=/etc/personal-agent/keys/api
MCP_KEYS=/etc/personal-agent/keys/mcp
API_ENV=/etc/personal-agent/api.env
MCP_ENV=/etc/personal-agent/mcp.env

API_FILES=(
  "$API_KEYS/agent-data.key"
  "$API_KEYS/agent-cursor.key"
  "$API_KEYS/agent-identifier.key"
  "$API_KEYS/agent-token.pem"
  "$API_KEYS/agent-service.pem"
  "$API_ENV"
)
MCP_FILES=(
  "$MCP_KEYS/agent-service.pub.pem"
  "$MCP_KEYS/finance-data.key"
  "$MCP_ENV"
)

for path in "${API_FILES[@]}" "${MCP_FILES[@]}"; do
  if [ -e "$path" ]; then
    echo "refusing to overwrite $path; remove it first if you mean to rotate" >&2
    exit 1
  fi
done

# Agent API rings: AES-256-GCM payload key, the two separate CAP-001 HMAC
# purposes, and two independent P-256 keys (access token, Host Context).
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" \
  > "$API_KEYS/agent-data.key"
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" \
  > "$API_KEYS/agent-cursor.key"
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" \
  > "$API_KEYS/agent-identifier.key"
openssl ecparam -name prime256v1 -genkey -noout -out "$API_KEYS/agent-token.pem" 2>/dev/null
openssl ecparam -name prime256v1 -genkey -noout -out "$API_KEYS/agent-service.pem" 2>/dev/null

# Finance MCP: the public half it verifies with, its own AES-256-GCM payload
# key, and an independent HMAC key for opaque expense-query cursors.
openssl ec -in "$API_KEYS/agent-service.pem" -pubout \
  -out "$MCP_KEYS/agent-service.pub.pem" 2>/dev/null
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" \
  > "$MCP_KEYS/finance-data.key"

chown root:"$API_USER" "$API_KEYS"/*
chmod 0640 "$API_KEYS"/*
chown root:"$MCP_USER" "$MCP_KEYS"/*
chmod 0640 "$MCP_KEYS"/*

cat > "$API_ENV" <<EOF
# DEV-032 environment for personal-agent-api. root:$API_USER 0640.
# PERSONAL_AGENT_USER_ID, ZAI_API_KEY and the optional
# PERSONAL_AGENT_LEDGER_URL are appended by the operator (deploy/README.md).
PERSONAL_AGENT_DATA_ACTIVE_KID=agent-data-ecs-1
PERSONAL_AGENT_DATA_ACTIVE_KEY_PATH=$API_KEYS/agent-data.key
PERSONAL_AGENT_CURSOR_ACTIVE_KID=agent-cursor-ecs-1
PERSONAL_AGENT_CURSOR_ACTIVE_KEY_PATH=$API_KEYS/agent-cursor.key
PERSONAL_AGENT_IDENTIFIER_ACTIVE_KID=agent-identifier-ecs-1
PERSONAL_AGENT_IDENTIFIER_ACTIVE_KEY_PATH=$API_KEYS/agent-identifier.key
PERSONAL_AGENT_TOKEN_ACTIVE_KID=agent-token-ecs-1
PERSONAL_AGENT_TOKEN_ACTIVE_PRIVATE_KEY_PATH=$API_KEYS/agent-token.pem
PERSONAL_AGENT_SERVICE_ACTIVE_KID=agent-service-ecs-1
PERSONAL_AGENT_SERVICE_ACTIVE_PRIVATE_KEY_PATH=$API_KEYS/agent-service.pem
EOF

cat > "$MCP_ENV" <<EOF
# DEV-032 environment for personal-data-mcp. root:$MCP_USER 0640.
# The FEISHU_FINANCE_* lines are appended by the operator from the existing
# local .env.finance.local (deploy/README.md), never typed by hand. The query
# cursor secret is minted on this server and never printed.
PERSONAL_DATA_MCP_DATA_ACTIVE_KID=finance-data-ecs-1
PERSONAL_DATA_MCP_DATA_ACTIVE_KEY_PATH=$MCP_KEYS/finance-data.key
PERSONAL_DATA_MCP_SERVICE_ACTIVE_KID=agent-service-ecs-1
PERSONAL_DATA_MCP_SERVICE_ACTIVE_PUBLIC_KEY_PATH=$MCP_KEYS/agent-service.pub.pem
EOF
# Keep this independent key out of shell variables and xtrace output.
python3 -c 'import base64, os, sys; p=sys.argv[1]; f=open(p, "ab"); f.write(b"PERSONAL_DATA_MCP_QUERY_CURSOR_SECRET=" + base64.urlsafe_b64encode(os.urandom(32)) + b"\n"); f.flush(); os.fsync(f.fileno()); f.close()' "$MCP_ENV"

chown root:"$API_USER" "$API_ENV"
chmod 0640 "$API_ENV"
chown root:"$MCP_USER" "$MCP_ENV"
chmod 0640 "$MCP_ENV"

echo "Key material and environment skeletons created (nothing printed that is secret)."
echo "Next: deploy/README.md step 4 — append the Feishu / GLM / user-id lines."
