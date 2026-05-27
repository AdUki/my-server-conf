#!/bin/bash
set -e
SCRIPT_DIR="$(realpath "$(dirname "$0")")"
cd "$SCRIPT_DIR"
docker compose up --remove-orphans -d
