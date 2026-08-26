#!/usr/bin/env bash
# Idempotent install/update for AI Marketplace Monitor.
# Re-running on a provisioned host should be a no-op.
set -euo pipefail
SCRIPT_DIR="$(realpath "$(dirname "$0")")"
AMM_HOME="$HOME/.ai-marketplace-monitor"
VENV="$SCRIPT_DIR/.venv"
# Our own fork: upstream is missing ~11 fixes (login, notifications,
# geo-scoping, AI scoring) without which this tool does not work here.
FORK="git+https://github.com/AdUki/ai-marketplace-monitor.git@main"

mkdir -p "$AMM_HOME"

# 1. venv (system python is externally-managed, PEP 668)
if [ ! -x "$VENV/bin/python" ]; then
    echo "==> creating venv"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
fi

# 2. the package, from the fork
if [ ! -x "$VENV/bin/ai-marketplace-monitor" ] || [ "${1:-}" = "--upgrade" ]; then
    echo "==> installing ai-marketplace-monitor from fork"
    "$VENV/bin/pip" install --quiet --upgrade "$FORK"
else
    echo "==> package already installed (pass --upgrade to refresh)"
fi

# 3. Playwright browser. Cached in ~/.cache/ms-playwright, so this is a
#    no-op once present.
echo "==> ensuring chromium is installed"
"$VENV/bin/playwright" install chromium >/dev/null

# 4. config.toml: the tool ALWAYS reads $AMM_HOME/config.toml, so point that
#    at the committed file rather than keeping two copies in sync.
if [ ! -L "$AMM_HOME/config.toml" ]; then
    if [ -f "$AMM_HOME/config.toml" ]; then
        echo "==> backing up existing config.toml"
        mv "$AMM_HOME/config.toml" "$AMM_HOME/config.toml.pre-symlink.bak"
    fi
    ln -s "$SCRIPT_DIR/config.toml" "$AMM_HOME/config.toml"
    echo "==> linked config.toml -> repo"
fi

# 5. secrets
if [ ! -f "$AMM_HOME/secrets.env" ]; then
    cp "$SCRIPT_DIR/secrets.env.example" "$AMM_HOME/secrets.env"
    chmod 600 "$AMM_HOME/secrets.env"
    echo "==> created $AMM_HOME/secrets.env - FILL IT IN before starting"
fi

# 6. systemd user unit
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
if ! cmp -s "$SCRIPT_DIR/aimm.service" "$UNIT_DIR/aimm.service"; then
    cp "$SCRIPT_DIR/aimm.service" "$UNIT_DIR/aimm.service"
    systemctl --user daemon-reload
    echo "==> installed/updated aimm.service"
fi
systemctl --user enable aimm.service >/dev/null 2>&1 || true
# Keeps the service alive after logout and across reboots.
loginctl show-user "$(id -un)" -p Linger | grep -q 'Linger=yes' || sudo loginctl enable-linger "$(id -un)"

echo
echo "Done. Start it with:  systemctl --user restart aimm"
echo "Logs:                 journalctl --user -u aimm -f"
echo "Web UI:               http://<pi-ip>:8467"
