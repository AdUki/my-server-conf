#!/bin/bash
set -e
SCRIPT_DIR="$(realpath "$(dirname "$0")")"
cd "$SCRIPT_DIR"
source "$SCRIPT_DIR/.env"
source "$SCRIPT_DIR/_backup-mount.sh"

if [[ -t 1 ]]; then
	INTERACTIVE=1
	DOCKER_FLAGS="-t"
	BORG_FLAGS="--progress --verbose"
fi

mkdir -p "$UPLOAD_LOCATION/database-backup"

if [ -n "$INTERACTIVE" ]; then echo Backup Immich database; fi
docker exec $DOCKER_FLAGS immich_postgres pg_dumpall --clean --if-exists --username="$DB_USERNAME" > "$UPLOAD_LOCATION"/database-backup/immich-database.sql

if [ -n "$INTERACTIVE" ]; then echo Append to local Borg repository; fi
borg create $BORG_FLAGS "$BACKUP_PATH/immich-borg::{now}" "$UPLOAD_LOCATION" --exclude "$UPLOAD_LOCATION"/thumbs/ --exclude "$UPLOAD_LOCATION"/encoded-video/
borg prune $BORG_FLAGS --keep-weekly=4 --keep-monthly=3 "$BACKUP_PATH"/immich-borg
borg compact $BORG_FLAGS "$BACKUP_PATH"/immich-borg
