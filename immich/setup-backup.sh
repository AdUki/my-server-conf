#!/bin/bash
set -e
SCRIPT_DIR="$(realpath "$(dirname "$0")")"
cd "$SCRIPT_DIR"
source "$SCRIPT_DIR/.env"
source "$SCRIPT_DIR/_backup-mount.sh"

if ! command -v borg >/dev/null; then
	sudo apt-get install -y borgbackup
fi

mkdir -p "$UPLOAD_LOCATION/database-backup"
sudo mkdir -p "$BACKUP_PATH"
sudo chown -fh pi:pi "$BACKUP_PATH" 2>/dev/null || true

if borg list "$BACKUP_PATH/immich-borg" >/dev/null 2>&1; then
	echo "Borg repo already initialised at $BACKUP_PATH/immich-borg"
else
	borg init --encryption=none "$BACKUP_PATH/immich-borg"
fi
