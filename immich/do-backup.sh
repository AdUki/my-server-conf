#!/bin/bash

set -e

source .env

if [[ -t 1 ]]; then
	INTERACTIVE=1
	FLAGS="--progress --verbose"
fi

if [ $INTERACTIVE ]; then echo Backup Immich database; fi
docker exec -t immich_postgres pg_dumpall --clean --if-exists --username=$DB_USERNAME > "$UPLOAD_LOCATION"/database-backup/immich-database.sql

if [ $INTERACTIVE ]; then echo Append to local Borg repository; fi
borg create $FLAGS "$BACKUP_PATH/immich-borg::{now}" "$UPLOAD_LOCATION" --exclude "$UPLOAD_LOCATION"/thumbs/ --exclude "$UPLOAD_LOCATION"/encoded-video/
borg prune $FLAGS --keep-weekly=4 --keep-monthly=3 "$BACKUP_PATH"/immich-borg
borg compact $FLAGS "$BACKUP_PATH"/immich-borg
if [ $INTERACTIVE ]; then echo Append to local Borg repository; fi
