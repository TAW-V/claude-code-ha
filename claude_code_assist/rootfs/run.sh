#!/usr/bin/with-contenv bash
# Claude Code Assist: installs Claude Code into persistent /data and runs the bridge.
set -euo pipefail

OPTS=/data/options.json
export HOME=/data/home
export PATH="$HOME/.local/bin:$PATH"
export DISABLE_AUTOUPDATER=1
mkdir -p "$HOME" /data/workspace

TOKEN_OPT=$(jq -r '.oauth_token // ""' "$OPTS")
if [ -n "$TOKEN_OPT" ]; then export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN_OPT"; fi
export CCA_MODEL=$(jq -r '.model // "sonnet"' "$OPTS")
export CCA_EFFORT=$(jq -r '.effort // ""' "$OPTS")
export CCA_TIMEOUT=$(jq -r '.timeout_seconds // 180' "$OPTS")
export CCA_COMPACT_HOUR=$(jq -r '.compact_hour // 4' "$OPTS")
export CCA_FOLLOWUP_WAIT=$(jq -r '.followup_wait_seconds // 600' "$OPTS")
export CCA_FOLLOWUP_NOTIFY=$(jq -r '.followup_notify // ""' "$OPTS")
export CCA_EXTRA=$(jq -r '.extra_instructions // ""' "$OPTS")

# Install Claude Code once; it lives in /data so it survives restarts and updates.
if ! command -v claude >/dev/null 2>&1; then
  echo "[cca] installing Claude Code into $HOME ..."
  curl -fsSL https://claude.ai/install.sh | bash
fi
echo "[cca] $(claude --version 2>/dev/null || echo 'claude not available')"

# Shared secret for the HA integration. HA core reads it from /config.
if [ ! -s /data/bridge_token ]; then
  python3 -c "import secrets;print(secrets.token_urlsafe(32))" > /data/bridge_token
fi
install -m 600 /data/bridge_token /config/.claude_code_assist_token

# Assistant instructions (CLAUDE.md): edited in the app's web UI; built-in default on first start.
if [ ! -s /data/workspace/CLAUDE.md ]; then cp /defaults/CLAUDE.md /data/workspace/CLAUDE.md; fi

if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ ! -f "$HOME/.claude/.credentials.json" ]; then
  echo "[cca] WARNING: no oauth_token set. Run 'claude setup-token' and paste the token in the app options."
fi

exec python3 /bridge.py
