#!/bin/bash
set -e
SCRIPT_DIR="$(realpath "$(dirname "$0")")"
cd "$SCRIPT_DIR"
source "$SCRIPT_DIR/.env"
source "$SCRIPT_DIR/_backup-mount.sh"

borg info "$BACKUP_PATH"/immich-borg

echo ''
echo '============= List of archives ============='
borg list -v "$BACKUP_PATH"/immich-borg
