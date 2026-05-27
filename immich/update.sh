#!/bin/bash
set -e
SCRIPT_DIR="$(realpath "$(dirname "$0")")"
cd "$SCRIPT_DIR"
"$SCRIPT_DIR/stop.sh"
docker compose pull
docker compose up -d
