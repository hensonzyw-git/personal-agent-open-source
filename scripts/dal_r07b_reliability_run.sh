#!/bin/bash
# DAL-R07B live reliability runner — feeds DAL_CODER_TOKEN without echo.
#
# Usage: bash scripts/dal_r07b_reliability_run.sh [token-file] [-- python-args...]
#
# Default token source is the same identity-token file the claude CLI uses for
# the local CCR proxy (settings.json ANTHROPIC_IDENTITY_TOKEN_FILE), so the
# coder child presents exactly the credential claude itself presents to CCR.
# The value never enters a shell echo, a log, or a conversation transcript.
# Any argument starting with "--" (and everything after) is forwarded to the
# python slice script instead of being treated as a token file path.
set -euo pipefail
cd "$(dirname "$0")/.."

TOKEN_FILE="$HOME/.claude-code-router/bin/ccr-claude-code-wif-token-default-claude-code"
for arg in "$@"; do
  if [[ "$arg" == --* ]]; then break; fi
  TOKEN_FILE="$arg"
  break
done
PY_ARGS=()
started=false
for arg in "$@"; do
  if [[ "$arg" == --* ]]; then started=true; fi
  if $started; then PY_ARGS+=("$arg"); fi
done

if [ -f "$TOKEN_FILE" ]; then
  # Fail closed on a loose source file: the coders' launcher requires 0600 on
  # the file it reads, and a loose token file here would silently widen the
  # credential's exposure before it is ever copied.
  perms=$(stat -f '%Lp' "$TOKEN_FILE")
  if [ "$perms" != "600" ]; then
    echo "token source file must be owner-only (0600), got $perms" >&2
    exit 1
  fi
  export DAL_CODER_TOKEN="$(tr -d ' \n\r' < "$TOKEN_FILE")"
else
  printf 'token file %s not found; enter appkey: ' "$TOKEN_FILE" >&2
  read -rs DAL_CODER_TOKEN
  echo >&2
  export DAL_CODER_TOKEN
fi

if [ -z "$DAL_CODER_TOKEN" ]; then
  echo "DAL_CODER_TOKEN is empty" >&2
  exit 1
fi

exec uv run python scripts/dal_r07b_reliability_slice.py "${PY_ARGS[@]}"
