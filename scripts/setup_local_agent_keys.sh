#!/usr/bin/env bash
# Create the Agent API's local key material, for development on this machine.
#
# `DEV-027`/`DEV-028`. The service loads every key from the environment and never
# opens a credential file itself, so something has to put the material on disk
# first. In production that is systemd credentials; locally it is this script.
#
# It refuses to overwrite an existing key: rotating one is a deliberate act with
# consequences (a replaced token key invalidates every issued access token, and a
# replaced service key stops Finance MCP verifying the Host Context until its own
# public copy is replaced too), so the removal has to be explicit.
#
# Nothing here is committed: `config/` and `*.pem` / `*.key` are Git-ignored.

set -euo pipefail

# Every private file must be born private. A chmod at the end is too late: with
# the normal macOS umask (022), the shell redirection below creates the data key
# as 0644, and any intervening OpenSSL failure would leave it that way.
umask 077

cd "$(dirname "$0")/.."
mkdir -p config

data_key="config/agent-data.key"
cursor_key="config/agent-cursor.key"
identifier_key="config/agent-identifier.key"
token_pem="config/agent-token.pem"
service_pem="config/agent-service.pem"
service_pub="config/agent-service.pub.pem"

for path in "$data_key" "$cursor_key" "$identifier_key" "$token_pem" "$service_pem"; do
  if [ -e "$path" ]; then
    echo "refusing to overwrite $path; remove it first if you mean to rotate" >&2
    exit 1
  fi
done

# AES-256-GCM payload key, base64url, exactly as `load_agent_data_keyring` reads it.
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" \
  > "$data_key"

# `CAP-001` design 5.6: the pagination cursor signer and the identifier/lineage
# HMAC are separate purposes and separate material. The loaders compare the
# bytes, so copying one file to the other name is refused rather than silently
# collapsing two purposes into one secret.
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" \
  > "$cursor_key"
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" \
  > "$identifier_key"

# Two independent P-256 keys: access tokens and the Host Context are signed with
# separate material so a compromise of one cannot mint the other's tokens.
openssl ecparam -name prime256v1 -genkey -noout -out "$token_pem" 2>/dev/null
openssl ecparam -name prime256v1 -genkey -noout -out "$service_pem" 2>/dev/null
openssl ec -in "$service_pem" -pubout -out "$service_pub" 2>/dev/null

chmod 600 "$data_key" "$cursor_key" "$identifier_key" "$token_pem" "$service_pem"
chmod 644 "$service_pub"

cat <<EOF
Created (private files mode 600; public key mode 644; all Git-ignored):
  $data_key
  $cursor_key
  $identifier_key
  $token_pem
  $service_pem
  $service_pub   (public half; Finance MCP verifies the Host Context with it)

Export for the Agent API:
  export PERSONAL_AGENT_DATA_ACTIVE_KID=agent-data-local
  export PERSONAL_AGENT_DATA_ACTIVE_KEY_PATH=\$PWD/$data_key
  export PERSONAL_AGENT_CURSOR_ACTIVE_KID=agent-cursor-local
  export PERSONAL_AGENT_CURSOR_ACTIVE_KEY_PATH=\$PWD/$cursor_key
  # During rotation: PERSONAL_AGENT_CURSOR_PREVIOUS_KEYS='old-kid=/old/path'
  export PERSONAL_AGENT_IDENTIFIER_ACTIVE_KID=agent-identifier-local
  export PERSONAL_AGENT_IDENTIFIER_ACTIVE_KEY_PATH=\$PWD/$identifier_key
  # During rotation: PERSONAL_AGENT_IDENTIFIER_PREVIOUS_KEYS='old-kid=/old/path'
  export PERSONAL_AGENT_TOKEN_ACTIVE_KID=agent-token-local
  export PERSONAL_AGENT_TOKEN_ACTIVE_PRIVATE_KEY_PATH=\$PWD/$token_pem
  export PERSONAL_AGENT_SERVICE_ACTIVE_KID=agent-service-local
  export PERSONAL_AGENT_SERVICE_ACTIVE_PRIVATE_KEY_PATH=\$PWD/$service_pem

And for Finance MCP, so it can verify what the Agent signs:
  export PERSONAL_DATA_MCP_SERVICE_ACTIVE_KID=agent-service-local
  export PERSONAL_DATA_MCP_SERVICE_ACTIVE_PUBLIC_KEY_PATH=\$PWD/$service_pub
EOF
