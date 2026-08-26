#!/usr/bin/env bash
# Launch AI Marketplace Monitor. Self-locating, so it works from anywhere
# (same convention as immich/*.sh).
set -euo pipefail
SCRIPT_DIR="$(realpath "$(dirname "$0")")"
AMM_HOME="$HOME/.ai-marketplace-monitor"
SECRETS_FILE="$AMM_HOME/secrets.env"

if [ ! -f "$SECRETS_FILE" ]; then
    echo "Missing $SECRETS_FILE - copy secrets.env.example there and fill it in." >&2
    exit 1
fi
set -a
# shellcheck disable=SC1090
source "$SECRETS_FILE"
set +a

# --headless: no display on the Pi. --webui-host 0.0.0.0: reachable on LAN
# and over Tailscale (it is deliberately NOT proxied to a public subdomain;
# its login gate uses the monitored Facebook account's own credentials).
exec "$SCRIPT_DIR/.venv/bin/ai-marketplace-monitor" \
    --headless --webui-host 0.0.0.0 "$@"
